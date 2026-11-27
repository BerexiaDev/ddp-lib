"""I05 target enumeration contracts for the canonical registry."""

from copy import deepcopy
import unittest
from unittest.mock import Mock

from ddp_lib.dataset_indexes import DEPENDENCIES, KINDS, Dataset, DatasetIndexes
from ddp_lib.index_metadata import IndexConflict


class PopulationDependencyTests(unittest.TestCase):
    def setUp(self):
        self.documents = {name: [] for name in KINDS.values()}
        self.documents["populations"] = [
            {"_id": "report", "schema": "populations", "table_name": "Report"},
            {"_id": "shared", "schema": "populations", "table_name": "Input"},
        ]
        self.documents["references"] = [
            {
                "_id": "shared",
                "data_mart_schema": "Exact Schema",
                "table_name": "Input",
            },
        ]
        self.documents["cleaned_tables"] = [
            {"_id": "shared", "schema": "data_cleaning", "table_name": "Input"},
        ]
        self.database = {}
        for name in KINDS.values():
            collection = Mock()
            collection.name = name
            collection.full_name = "i05." + name

            def find(query, name=name):
                return [
                    deepcopy(doc)
                    for doc in self.documents[name]
                    if all(doc.get(key) == value for key, value in query.items())
                ]

            collection.find.side_effect = find
            collection.find_one.side_effect = lambda query, find=find: next(
                iter(find(query)), None
            )
            self.database[name] = collection
        self.connector_factory = Mock(
            side_effect=AssertionError("Enumeration must not open SQL connections")
        )
        self.manager = DatasetIndexes(
            self.database, self.connector_factory, lambda doc: doc["data_mart_schema"]
        )
        self.population = self.documents["populations"][0]

    def test_I05_B_D_placeholders_duplicates_and_self_reference(self):
        for field in DEPENDENCIES:
            self.population[field] = [None, "shared", "None", "shared", None]
        self.population["population_ids"] += ["report", "report"]
        before = deepcopy(self.population)
        targets = self.manager.population_targets("report")
        self.assertTrue(all(isinstance(target, Dataset) for target in targets))
        self.assertEqual(
            [(t.kind, t.id) for t in targets],
            [
                ("POPULATION", "report"),
                ("REFERENCE", "shared"),
                ("POPULATION", "shared"),
                ("CLEANED_TABLE", "shared"),
            ],
        )
        self.assertEqual(self.population, before)
        self.assertEqual(
            self.manager.resolve("Exact Schema", "Input", targets).kind, "REFERENCE"
        )
        with self.assertRaises(ValueError):
            self.manager.resolve("exact schema", "input", targets)

    def test_I05_B_empty_or_absent_arrays_keep_own_target(self):
        for value in (None, [], [None, "None"]):
            with self.subTest(value=value):
                for field in DEPENDENCIES:
                    self.population[field] = value
                self.assertEqual(
                    [(t.kind, t.id) for t in self.manager.population_targets("report")],
                    [("POPULATION", "report")],
                )
        for field in DEPENDENCIES:
            self.population.pop(field)
        self.assertEqual(len(self.manager.population_targets("report")), 1)

    def test_I05_C_only_exact_legacy_placeholders_are_ignored(self):
        for field, kind in DEPENDENCIES.items():
            for missing in (
                "missing-id",
                "none",
                "NONE",
                "null",
                " None ",
                "missing'quoted",
            ):
                with self.subTest(field=field, missing=missing):
                    with self.assertRaises(LookupError) as error:
                        self.manager.inputs({field: [None, "None", "shared", missing]})
                    self.assertEqual(
                        str(error.exception),
                        f"Index target dataset not found: {kind} {missing!r}",
                    )

    def test_invalid_ids_still_fail_validation(self):
        for field in DEPENDENCIES:
            for invalid in ("", 0, False, [], {}):
                with self.subTest(field=field, invalid=invalid):
                    with self.assertRaisesRegex(
                        ValueError, "dataset_id must be a non-empty string"
                    ):
                        self.manager.inputs({field: [invalid]})

    def test_missing_own_target_is_not_filtered(self):
        with self.assertRaises(LookupError) as error:
            self.manager.population_targets("None")
        self.assertEqual(
            str(error.exception), "Index target dataset not found: POPULATION 'None'"
        )

    def test_duplicate_physical_owners_still_fail(self):
        self.documents["references"].append(
            {
                "_id": "duplicate",
                "data_mart_schema": "Exact Schema",
                "table_name": "Input",
            }
        )
        with self.assertRaises(IndexConflict):
            self.manager.inputs({"tables_ids": [None, "None", "shared"]})


if __name__ == "__main__":
    unittest.main()

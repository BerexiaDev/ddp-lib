"""I07 saved source policy contracts; database cases use disposable PG/Mongo."""

import copy
import os
import unittest
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

from ddp_lib.dataset_indexes import Dataset, DatasetIndexes, source_entries
from ddp_lib.index_metadata import IndexConflict, save_intent, delete_intent
from ddp_lib.index_sql import parse_index
from ddp_lib.postgres_indexes import inspect_index, index_ready, list_indexes
from ddp_lib.secondary_indexes import IndexRepairError, apply_entry, owns


def target(policies, secondary=()):
    return Dataset(
        "REFERENCE",
        None,
        {
            "_id": "reference",
            "source_indexes": policies,
            "secondary_indexes": list(secondary),
        },
        "schema",
        "table",
    )


class SourcePolicyTests(unittest.TestCase):
    def test_I07_D_collisions_duplicates_and_order_have_stable_names(self):
        policies = [
            {"columns": ["a_b", "c"]},
            {"columns": ["a", "b_c"]},
            {"columns": ["A", "B"]},
            {"columns": ["a", "b"]},
            {"columns": ["b", "a"]},
            {"columns": ["a", "b"], "unique": True},
        ]
        first = source_entries(target(policies))
        second = source_entries(target(list(reversed(policies)) + [policies[0]]))
        self.assertEqual(
            {e["id"]: e["index_name"] for e in first},
            {e["id"]: e["index_name"] for e in second},
        )
        self.assertEqual(len({e["index_name"] for e in first}), len(policies))
        self.assertEqual(len(second), len(first))

    def test_short_names_and_existing_long_name_identity_are_preserved(self):
        self.assertEqual(
            source_entries(target([{"columns": ["id"]}]))[0]["index_name"],
            "idx_schema_table_id",
        )
        policies = [{"columns": ["x" * 60]}, {"columns": ["x" * 59 + "y"]}]
        first = source_entries(target(policies))
        self.assertEqual(first, source_entries(target(policies)))
        self.assertEqual(len({e["index_name"] for e in first}), 2)
        self.assertTrue(all(len(e["index_name"].encode()) <= 63 for e in first))

    def test_exact_keys_are_not_trimmed_or_lowercased(self):
        entries = source_entries(
            target([{"columns": [" ID ", 'é"Key']}, {"columns": ["ID", 'é"Key']}])
        )
        self.assertNotEqual(entries[0]["id"], entries[1]["id"])
        self.assertIn('" ID "', entries[0]["sql"])
        self.assertIn('"é""Key"', entries[0]["sql"])

    def test_I07_E_unsupported_definitions_are_explicit(self):
        malformed = [
            {},
            {"columns": "id"},
            {"columns": []},
            {"columns": [None]},
            {"columns": [{"expression": "lower(id)"}]},
            {"columns": [""]},
            {"columns": ["a" * 64]},
            {"columns": ["id"], "unique": "false"},
            {"columns": ["id"], "predicate": "id > 0"},
            {"columns": ["id"], "method": "gin"},
            {"columns": ["id"], "include": ["x"]},
            {"columns": ["id"], "expressions": ["lower(id)"]},
            "id",
        ]
        for policy in malformed:
            with self.subTest(policy=policy), self.assertRaises(ValueError):
                source_entries(target([{"columns": ["valid"]}, policy]))
        for policies in ({}, "", False):
            with self.subTest(policies=policies), self.assertRaises(ValueError):
                source_entries(target(policies))

    def test_empty_missing_and_primary_only_are_read_only(self):
        for policies in (
            [],
            None,
            [{"primary": True}],
            [{"columns": ["id"], "source_idxtype": "U"}],
        ):
            dataset = target(policies)
            original = copy.deepcopy(dataset.document)
            entries = source_entries(dataset)
            self.assertEqual(dataset.document, original)
            self.assertEqual(
                len(entries), int(bool(policies and not policies[0].get("primary")))
            )

    def test_generated_name_cannot_acquire_secondary_ownership(self):
        policies = [{"columns": ["id"]}]
        name = source_entries(target(policies))[0]["index_name"]
        with self.assertRaisesRegex(IndexConflict, "secondary intent"):
            source_entries(
                target(policies, [{"index_name": name, "desired_state": "absent"}])
            )


@unittest.skipUnless(
    os.getenv("QUEUE_TEST_POSTGRES_DSN") and os.getenv("QUEUE_TEST_MONGO_URI"),
    "Set QUEUE_TEST_POSTGRES_DSN and QUEUE_TEST_MONGO_URI to disposable databases",
)
class SourcePolicyDatabaseTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        from pymongo import MongoClient

        self.connect = lambda: psycopg2.connect(os.environ["QUEUE_TEST_POSTGRES_DSN"])
        self.connection = self.connect()
        self.connection.autocommit = True
        self.cursor = self.connection.cursor()
        self.schema = "i07_" + uuid4().hex[:12]
        self.table = f'"{self.schema}".data'
        self.cursor.execute(f'CREATE SCHEMA "{self.schema}"')
        self.cursor.execute(
            f"CREATE TABLE {self.table} (id int, a_b int, c int, a int, b_c int)"
        )
        self.client = MongoClient(os.environ["QUEUE_TEST_MONGO_URI"])
        self.db = self.client["i07_" + uuid4().hex]
        self.db.references.insert_one(
            {
                "_id": "reference",
                "data_mart_schema": self.schema,
                "table_name": "data",
                "source_indexes": [],
            }
        )
        self.manager = DatasetIndexes(
            self.db,
            lambda schema: SimpleNamespace(get_connection=self.connect),
            lambda doc: doc["data_mart_schema"],
        )

    def tearDown(self):
        self.cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.cursor.close()
        self.connection.close()
        self.client.drop_database(self.db.name)
        self.client.close()

    def target(self, policies=None):
        if policies is not None:
            self.db.references.update_one(
                {"_id": "reference"}, {"$set": {"source_indexes": policies}}
            )
        return self.manager.get("REFERENCE", "reference")

    def physical(self, name):
        return inspect_index(self.cursor, self.schema, name)

    def test_I07_C_owned_historical_name_reused_without_duplicate(self):
        target = self.target([{"columns": ["a_b", "c"], "unique": True}])
        self.manager.reconcile(target)
        original = self.manager.source_policy(target, self.connection)[0]["index_name"]
        oid = self.physical(original).oid
        self.cursor.execute(
            f'ALTER INDEX "{self.schema}"."{original}" RENAME TO historical'
        )
        self.manager.reconcile(target)
        bound = self.manager.source_policy(target, self.connection)
        self.assertEqual([e["index_name"] for e in bound], ["historical"])
        self.assertEqual(self.physical("historical").oid, oid)
        self.assertIsNone(self.physical(original))
        self.assertTrue(owns(bound[0], self.physical("historical")))
        self.assertEqual(len(list_indexes(self.cursor, self.schema, "data")), 1)

    def test_historical_equivalence_respects_order_uniqueness_and_adoption(self):
        target = self.target([{"columns": ["a_b", "c"], "unique": True}])
        for name, keys, unique in [
            ("wrong_order", "c,a_b", True),
            ("not_unique", "a_b,c", False),
            ("historical", "a_b,c", True),
        ]:
            self.cursor.execute(
                f'CREATE {"UNIQUE " if unique else ""}INDEX {name} ON {self.table} ({keys})'
            )
        bound = self.manager.source_policy(target, self.connection)[0]
        self.assertEqual(bound["index_name"], "historical")
        with self.assertRaises(IndexRepairError):
            self.manager.reconcile(target)
        self.assertIsNone(self.physical("historical").comment)
        apply_entry(self.cursor, dict(bound, adopt=True), self.schema, "data")
        self.manager.reconcile(target)
        self.assertEqual(len(list_indexes(self.cursor, self.schema, "data")), 3)

    def test_secondary_equivalent_and_receipt_never_become_source(self):
        sql = f"CREATE INDEX secondary ON {self.table} (id)"
        save_intent(self.db.references, "reference", self.schema, "data", sql)
        self.manager.reconcile(self.target())
        oid = self.physical("secondary").oid
        target = self.target([{"columns": ["id"]}])
        self.manager.reconcile(target)
        self.assertNotEqual(
            self.manager.source_policy(target, self.connection)[0]["index_name"],
            "secondary",
        )
        self.assertEqual(self.physical("secondary").oid, oid)
        # Even if metadata is lost, a foreign receipt cannot silently change owners.
        self.db.references.update_one(
            {"_id": "reference"}, {"$set": {"secondary_indexes": []}}
        )
        entries = self.manager.source_policy(self.target(), self.connection)
        self.assertNotEqual(entries[0]["index_name"], "secondary")

    def test_I07_D_ambiguous_old_name_is_not_dropped(self):
        policies = [{"columns": ["a_b", "c"]}, {"columns": ["a", "b_c"]}]
        target = self.target(policies)
        ambiguous = f"idx_{self.schema}_data_a_b_c"
        self.cursor.execute(f'CREATE INDEX "{ambiguous}" ON {self.table} (id)')
        oid = self.physical(ambiguous).oid
        self.manager.reconcile(target)
        entries = self.manager.source_policy(target, self.connection)
        self.assertEqual(len({e["index_name"] for e in entries}), 2)
        self.assertTrue(all(self.physical(e["index_name"]).valid for e in entries))
        self.assertEqual(self.physical(ambiguous).oid, oid)
        self.assertIsNone(self.physical(ambiguous).comment)
        self.assertEqual(self.db.references.find_one()["source_indexes"], policies)

    def test_I07_B_restoration_preserves_source_and_secondary_intent(self):
        policies = [{"columns": ["a_b", "c"]}]
        target = self.target(policies)
        entry = save_intent(
            self.db.references,
            "reference",
            self.schema,
            "data",
            f"CREATE INDEX secondary ON {self.table} (id)",
        )
        self.manager.reconcile(target)
        for _ in range(2):
            self.cursor.execute(f"DROP TABLE {self.table}")
            self.cursor.execute(f"CREATE TABLE {self.table} (id int, a_b int, c int)")
            self.manager.reconcile(self.target())
            doc = self.db.references.find_one()
            self.assertEqual(doc["source_indexes"], policies)
            for key in ("id", "sql", "revision", "desired_state"):
                self.assertEqual(doc["secondary_indexes"][0][key], entry[key])
        delete_intent(self.db.references, "reference", self.schema, "secondary")
        self.manager.reconcile(self.target())
        self.assertIsNone(self.physical("secondary"))

    def partition_tree(self):
        self.cursor.execute(f"DROP TABLE {self.table}")
        self.cursor.execute(
            f"CREATE TABLE {self.table} (id int, a int) PARTITION BY RANGE(id)"
        )
        self.cursor.execute(
            f'CREATE TABLE "{self.schema}".branch PARTITION OF {self.table} '
            "FOR VALUES FROM(0) TO(100) PARTITION BY RANGE(id)"
        )
        self.cursor.execute(
            f'CREATE TABLE "{self.schema}".leaf PARTITION OF "{self.schema}".branch '
            "FOR VALUES FROM(0) TO(100)"
        )
        return self.target([{"columns": ["id"]}])

    def test_partition_parent_only_rendering_reuses_complete_historical_tree(self):
        target = self.partition_tree()
        self.manager.reconcile(target)
        entry = self.manager.source_policy(target, self.connection)[0]
        self.cursor.execute(
            f'ALTER INDEX "{self.schema}"."{entry["index_name"]}" RENAME TO historical'
        )
        self.assertIn("ON ONLY", self.physical("historical").definition)
        self.manager.reconcile(target)
        self.assertTrue(index_ready(self.cursor, self.physical("historical")))
        self.assertFalse(
            parse_index(
                self.manager.source_policy(target, self.connection)[0]["sql"]
            ).only
        )
        self.assertEqual(len(list_indexes(self.cursor, self.schema, "data")), 1)

    def test_partition_tree_readiness_checks_descendants_even_if_parent_valid(self):
        target = self.partition_tree()
        self.manager.reconcile(target)
        entry = self.manager.source_policy(target, self.connection)[0]
        parent = self.physical(entry["index_name"])
        leaf = list_indexes(self.cursor, self.schema, "leaf")[0]
        # Fault injection is restricted to this disposable schema's leaf index.
        self.cursor.execute(
            "UPDATE pg_index SET indisready=false WHERE indexrelid=%s", (leaf.oid,)
        )
        self.assertTrue(self.physical(parent.index_name).valid)
        self.assertFalse(index_ready(self.cursor, parent))
        with self.assertRaisesRegex(IndexRepairError, "partition tree"):
            self.manager.reconcile(target)
        self.assertFalse(self.manager.listing(target)[0]["physical_ready"])
        self.assertTrue(self.db.references.find_one()["index_repair_pending"])
        self.cursor.execute(
            "UPDATE pg_index SET indisready=true WHERE indexrelid=%s", (leaf.oid,)
        )
        self.manager.reconcile(target)
        self.assertEqual(self.physical(parent.index_name).oid, parent.oid)

    def test_incomplete_partition_parent_requires_review_without_replacement(self):
        target = self.partition_tree()
        entry = source_entries(target)[0]
        self.cursor.execute(parse_index(entry["sql"]).render(only=True))
        parent = self.physical(entry["index_name"])
        self.assertFalse(index_ready(self.cursor, parent))
        with self.assertRaises(IndexRepairError):
            apply_entry(self.cursor, dict(entry, adopt=True), self.schema, "data")
        self.assertEqual(self.physical(parent.index_name).oid, parent.oid)


if __name__ == "__main__":
    unittest.main()

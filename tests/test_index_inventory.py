"""I06 acceptance on disposable stores; no application/production configuration."""

from contextlib import contextmanager
from copy import deepcopy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import psycopg2
from psycopg2 import extensions
from pymongo import MongoClient

from ddp_lib import index_inventory as inventory
from ddp_lib.dataset_indexes import DatasetIndexes
from ddp_lib.index_metadata import delete_intent, editable_fields, save_intent
from ddp_lib.postgres_indexes import inspect_index


class ConnectorComparisonTests(unittest.TestCase):
    def test_unknowns_are_null_and_every_private_setting_is_compared(self):
        base = dict(
            _id="a",
            type="postgres",
            host="192.0.2.10",
            port=5432,
            database="example",
            schema="Source",
            user="reader",
        )
        owners = [
            dict(
                dataset_type="REFERENCE",
                connector_id=key,
                connector=inventory.connector_context({}, dict(base, _id=key)),
            )
            for key in ("a", "b")
        ]
        settings = {"a": base, "b": dict(base, _id="b")}
        result = inventory.compare_connectors(owners, settings)
        self.assertIsNone(result["matching_passwords"])
        self.assertIsNone(result["matching_other_settings"])
        for key in settings:
            settings[key].update(
                password="synthetic-secret", future_tls_setting="synthetic-private"
            )
        self.assertEqual(
            inventory.compare_connectors(owners, settings)["classification"],
            "duplicate_connector_settings",
        )
        settings["b"]["future_tls_setting"] = "different-private"
        result = inventory.compare_connectors(owners, settings)
        self.assertFalse(result["matching_other_settings"])
        self.assertNotIn("synthetic", json.dumps(result))
        settings["b"]["password"] = None
        self.assertIsNone(
            inventory.compare_connectors(owners, settings)["matching_passwords"]
        )

    def test_retirement_flag_is_protected(self):
        self.assertEqual(
            editable_fields(
                "populations",
                {
                    "legacy_indexes_retired": False,
                    "legacy_indexes_retired.x": 0,
                    "name": "ok",
                },
            ),
            {"name": "ok"},
        )


@unittest.skipUnless(
    os.getenv("QUEUE_TEST_POSTGRES_DSN") and os.getenv("QUEUE_TEST_MONGO_URI"),
    "Set QUEUE_TEST_POSTGRES_DSN and QUEUE_TEST_MONGO_URI to disposable databases",
)
class InventoryTests(unittest.TestCase):
    def setUp(self):
        self.client = MongoClient(os.environ["QUEUE_TEST_MONGO_URI"])
        self.db = self.client["i06_" + uuid4().hex]
        self.schema = "i06_" + uuid4().hex[:12]
        self.statements = []
        statements = self.statements

        class Cursor(extensions.cursor):
            def execute(self, statement, parameters=None):
                statements.append(
                    statement
                    if isinstance(statement, str)
                    else statement.as_string(self)
                )
                return super().execute(statement, parameters)

        self.connection = psycopg2.connect(
            os.environ["QUEUE_TEST_POSTGRES_DSN"], cursor_factory=Cursor
        )
        self.connection.autocommit = True
        self.cur = self.connection.cursor()
        self.cur.execute(f'CREATE SCHEMA "{self.schema}"')
        self.cur.execute(
            f'CREATE TABLE "{self.schema}".data (id int PRIMARY KEY, value text, extra int)'
        )
        self.cur.execute(f'CREATE INDEX manual ON "{self.schema}".data (value)')
        self.connector = dict(
            _id="connector",
            name="Synthetic source",
            type="postgres",
            host="192.0.2.10",
            port=5432,
            database="fixture",
            schema="Source",
            user="reader",
            password="never-export-password",
            conn_string="never-export-connection-string",
        )
        self.db.connectors.insert_one(deepcopy(self.connector))
        self.db.references.insert_one(
            dict(
                _id="reference",
                name="Synthetic reference",
                status="COMPLETED",
                data_mart_schema=self.schema,
                table_name="data",
                connector_id="connector",
                source_indexes=[{"columns": ["id"]}],
            )
        )
        self.manager = DatasetIndexes(self.db, None, inventory.explicit_schema)
        self.statements.clear()

    def tearDown(self):
        self.cur.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.cur.close()
        self.connection.close()
        self.client.drop_database(self.db.name)
        self.client.close()

    def snapshot(self):
        return {
            name: list(self.db[name].find({}))
            for name in self.db.list_collection_names()
        }

    def extract(self):
        return inventory.extract_inventory(self.db, self.connection)

    def review(self):
        return inventory.prepare_inventory(self.extract())

    def apply(self, report):
        return inventory.apply_inventory(report, self.db, self.connection)

    def manual(self):
        return inspect_index(self.cur, self.schema, "manual")

    def duplicate(self, **changes):
        connector = dict(self.connector, _id="other-connector", **changes)
        self.db.connectors.insert_one(connector)
        doc = self.db.references.find_one({"_id": "reference"})
        self.db.references.insert_one(
            dict(doc, _id="other", connector_id="other-connector")
        )

    def test_extraction_is_read_only_and_keeps_every_catalog_class(self):
        self.cur.execute(f'CREATE INDEX old_source_name ON "{self.schema}".data (id)')
        self.cur.execute(
            f'CREATE INDEX expression_partial ON "{self.schema}".data ((lower(value))) INCLUDE (extra) WHERE extra > 0'
        )
        self.cur.execute(
            f'CREATE MATERIALIZED VIEW "{self.schema}".view_data AS SELECT * FROM "{self.schema}".data'
        )
        self.cur.execute(
            f'CREATE INDEX view_index ON "{self.schema}".view_data (value)'
        )
        self.cur.execute(
            f'CREATE TABLE "{self.schema}".parts (id int) PARTITION BY RANGE (id)'
        )
        self.cur.execute(
            f'CREATE TABLE "{self.schema}".part1 PARTITION OF "{self.schema}".parts FOR VALUES FROM (0) TO (10)'
        )
        self.cur.execute(f'CREATE INDEX partition_index ON "{self.schema}".parts (id)')
        self.db.populations.insert_one(
            dict(_id="view", schema=self.schema, table_name="view_data")
        )
        self.db.cleaned_tables.insert_one(
            dict(_id="parts", schema=self.schema, table_name="parts")
        )
        self.db.cleaned_tables.insert_one(
            dict(_id="part1", schema=self.schema, table_name="part1")
        )
        self.cur.execute(
            f"INSERT INTO \"{self.schema}\".data VALUES (1, 'duplicate', 0), (2, 'duplicate', 0)"
        )
        with self.assertRaises(psycopg2.errors.UniqueViolation):
            self.cur.execute(
                f'CREATE UNIQUE INDEX CONCURRENTLY invalid_index ON "{self.schema}".data (value)'
            )
        before, physical = self.snapshot(), self.manual()
        self.statements.clear()
        report = self.extract()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(physical, self.manual())
        self.assertFalse(
            any(
                s.startswith(("CREATE", "DROP", "ALTER", "COMMENT", "INSERT", "UPDATE"))
                for s in self.statements
            )
        )
        observed = {
            i["index_name"]: i for r in report["relations"] for i in r["indexes"]
        }
        self.assertTrue(observed["data_pkey"]["primary"])
        self.assertFalse(observed["invalid_index"]["valid"])
        self.assertEqual(observed["partition_index"]["relation_kind"], "p")
        self.assertIn("part1_id_idx", observed)
        self.assertIn("old_source_name", observed)
        self.assertEqual(report["summary"]["physical_indexes"], 8)
        prepared = inventory.prepare_inventory(report)
        selected = {i["index_name"] for i in prepared["items"] if i["approved"]}
        # A partition leaf is an ordinary table, but its attached index is owned
        # by the parent tree and must be left for an explicit tree review.
        self.assertEqual(selected, {"manual", "expression_partial", "view_index"})
        self.assertLessEqual(
            report["extraction_started_at"], report["extraction_completed_at"]
        )

    def test_shared_target_is_inspected_once_without_choosing_owner(self):
        self.duplicate()
        with patch.object(
            inventory, "relation_inventory", wraps=inventory.relation_inventory
        ) as read:
            report = self.extract()
        self.assertEqual(read.call_count, 1)
        comparison = report["ownership_conflicts"][0]["connector_comparison"]
        self.assertEqual(comparison["classification"], "duplicate_connector_settings")
        self.assertTrue(comparison["matching_passwords"])
        self.assertFalse(
            any(i["approved"] for i in inventory.prepare_inventory(report)["items"])
        )
        serialized = json.dumps(report)
        for secret in (self.connector["password"], self.connector["conn_string"]):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("password_hash", serialized)

    def test_different_unknown_credentials_and_source_overrides(self):
        self.duplicate(password="different-secret")
        comparison = lambda: self.extract()["ownership_conflicts"][0][
            "connector_comparison"
        ]
        self.assertFalse(comparison()["matching_passwords"])
        self.db.connectors.update_one(
            {"_id": "other-connector"}, {"$unset": {"password": ""}}
        )
        self.assertIsNone(comparison()["matching_passwords"])
        self.db.references.update_one(
            {"_id": "other"}, {"$set": {"source_schema": "OtherSource"}}
        )
        self.assertIn("schema", comparison()["differing_public_fields"])
        self.db.connectors.update_one(
            {"_id": "other-connector"}, {"$set": {"host": "192.0.2.11"}}
        )
        self.assertIn("host", comparison()["differing_public_fields"])
        self.db.references.update_one(
            {"_id": "other"}, {"$set": {"connector_id": "missing"}}
        )
        self.assertIsNone(comparison()["matching_passwords"])

    def test_missing_relation_connector_and_explicit_identity_are_distinct(self):
        self.db.references.update_one(
            {"_id": "reference"}, {"$set": {"table_name": "gone"}}
        )
        self.assertEqual(self.extract()["summary"]["missing_relations"], 1)
        self.db.references.update_one(
            {"_id": "reference"}, {"$set": {"connector_id": "gone"}}
        )
        self.assertIn("unresolved_connector", self.extract()["datasets"][0]["issues"])
        self.db.references.update_one(
            {"_id": "reference"}, {"$unset": {"data_mart_schema": ""}}
        )
        report = self.extract()
        self.assertIn(
            "unresolved_destination_identity", report["datasets"][0]["issues"]
        )
        self.assertEqual(report["relations"], [])

    def test_offline_review_preserves_issues_and_uses_catalog_sql(self):
        self.db.populations.insert_one(
            dict(
                _id="population",
                schema=self.schema,
                table_name="view_data",
                query_indexes=[
                    {"sql": f'CREATE INDEX manual ON "{self.schema}".data (extra)'},
                    {"sql": f'CREATE INDEX missing ON "{self.schema}".data (extra)'},
                ],
                indexes=["unknown"],
            )
        )
        report = self.extract()
        original = deepcopy(report)
        reviewed = inventory.prepare_inventory(report)
        self.assertEqual(report, original)
        selected = [i for i in reviewed["items"] if i["approved"]]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["sql"], self.manual().definition)
        self.assertIn("mismatched_definition", selected[0]["issues"])
        self.assertEqual(report["issues"], reviewed["issues"])
        self.assertIn("missing_legacy_index", reviewed["review_counts"])
        self.assertEqual(
            reviewed["legacy_retirements"],
            [
                {
                    "collection": "populations",
                    "dataset_id": "population",
                    "approved": True,
                }
            ],
        )

    def test_one_invalid_entry_prevents_all_metadata_writes(self):
        self.cur.execute(f'CREATE INDEX second ON "{self.schema}".data (extra)')
        report = self.review()
        self.cur.execute(f'DROP INDEX "{self.schema}".second')
        before = self.snapshot()
        with self.assertRaisesRegex(inventory.InventoryError, "missing_legacy_index"):
            self.apply(report)
        self.assertEqual(before, self.snapshot())

    def test_locked_revalidation_rejects_changed_catalog(self):
        report, before = self.review(), self.snapshot()
        original = DatasetIndexes.guard

        @contextmanager
        def change_under_lock(manager, target, connection):
            with original(manager, target, connection):
                other = psycopg2.connect(os.environ["QUEUE_TEST_POSTGRES_DSN"])
                other.autocommit = True
                try:
                    with other.cursor() as cursor:
                        cursor.execute(f'DROP INDEX "{self.schema}".manual')
                        cursor.execute(
                            f'CREATE INDEX manual ON "{self.schema}".data (extra)'
                        )
                finally:
                    other.close()
                yield connection

        with patch.object(DatasetIndexes, "guard", change_under_lock):
            with self.assertRaisesRegex(
                inventory.InventoryError, "catalog_definition_changed"
            ):
                self.apply(report)
        self.assertEqual(before, self.snapshot())

    def test_current_owner_source_policy_defaults_and_validity(self):
        report = self.review()
        self.duplicate()
        with self.assertRaisesRegex(inventory.InventoryError, "ownership"):
            self.apply(report)
        self.db.references.delete_one({"_id": "other"})
        self.db.references.update_one(
            {"_id": "reference"}, {"$set": {"source_indexes": [{"columns": ["value"]}]}}
        )
        with self.assertRaisesRegex(inventory.InventoryError, "source_policy"):
            self.apply(report)
        self.db.references.update_one(
            {"_id": "reference"}, {"$set": {"source_indexes": []}}
        )
        report["schema_defaults"]["IMPORT_DATA"] = "wrong"
        with self.assertRaisesRegex(
            inventory.InventoryError, "schema_defaults_changed"
        ):
            self.apply(report)
        report["schema_defaults"] = inventory.schema_defaults()
        self.cur.execute(
            "UPDATE pg_index SET indisready=false WHERE indexrelid=%s",
            (self.manual().oid,),
        )
        with self.assertRaisesRegex(inventory.InventoryError, "invalid_or_not_ready"):
            self.apply(report)
        self.assertNotIn("secondary_indexes", self.db.references.find_one())

    def test_live_connector_resolution_ignores_embedded_context(self):
        report = self.review()
        report["datasets"][0]["connector"]["type"] = "donor-only-engine"
        report["datasets"][0]["document"]["connector"] = {"database": "wrong"}
        self.db.references.update_one(
            {"_id": "reference"}, {"$set": {"connector": {"type": "donor-only-engine"}}}
        )
        self.db.connectors.delete_one({"_id": "connector"})
        with self.assertRaisesRegex(inventory.InventoryError, "unresolved_connector"):
            self.apply(report)
        self.db.connectors.insert_one(self.connector)
        self.assertEqual(self.apply(report).applied, 1)

    def test_metadata_only_rerun_preserves_intent_and_tombstone(self):
        report, physical = self.review(), self.manual()
        self.statements.clear()
        self.assertEqual(self.apply(report).applied, 1)
        self.assertFalse(
            any(
                s.startswith(("CREATE", "DROP", "COMMENT", "ALTER"))
                for s in self.statements
            )
        )
        self.assertEqual(physical, self.manual())
        doc = self.db.references.find_one()
        self.assertTrue(doc["index_repair_pending"])
        self.assertEqual(doc["secondary_indexes"][0]["status"], "pending")
        self.assertTrue(doc["secondary_indexes"][0]["adopt"])
        self.assertEqual(self.apply(report).applied, 0)
        self.assertEqual(doc, self.db.references.find_one())
        delete_intent(self.db.references, "reference", self.schema, "manual")
        tombstone = self.db.references.find_one()
        self.cur.execute(f'DROP INDEX "{self.schema}".manual')
        self.assertEqual(self.apply(report).applied, 0)
        self.assertEqual(tombstone, self.db.references.find_one())

    def test_existing_registry_and_tombstones_are_explained(self):
        save_intent(
            self.db.references,
            "reference",
            self.schema,
            "data",
            self.manual().definition,
        )
        for deleted in (False, True):
            if deleted:
                delete_intent(self.db.references, "reference", self.schema, "manual")
            item = next(
                i for i in self.review()["items"] if i["index_name"] == "manual"
            )
            self.assertFalse(item["approved"])
            self.assertEqual(item["review"], "existing_registry_entry_or_tombstone")

    def test_locked_revalidation_rechecks_connector_and_ownership(self):
        report = self.review()
        original = DatasetIndexes.guard
        for change, expected in (
            (
                lambda: self.db.connectors.delete_one({"_id": "connector"}),
                "unresolved_connector",
            ),
            (
                lambda: self.db.references.insert_one(
                    dict(_id="unresolved", table_name="data")
                ),
                "ownership",
            ),
        ):

            @contextmanager
            def change_under_lock(manager, target, connection):
                with original(manager, target, connection):
                    change()
                    yield connection

            with patch.object(DatasetIndexes, "guard", change_under_lock):
                with self.assertRaisesRegex(inventory.InventoryError, expected):
                    self.apply(report)
            self.assertNotIn(
                "secondary_indexes", self.db.references.find_one({"_id": "reference"})
            )
            self.db.connectors.replace_one(
                {"_id": "connector"}, self.connector, upsert=True
            )

    def test_review_cannot_enable_create_or_use_another_environment(self):
        report = self.review()
        report["migration_policy"] = "restore-missing"
        with self.assertRaisesRegex(inventory.InventoryError, "only_reviewed_existing"):
            self.apply(report)
        report["migration_policy"] = inventory.POLICY
        report["environment"]["metadata_database"] = "different"
        with self.assertRaisesRegex(
            inventory.InventoryError, "destination_environment_changed"
        ):
            self.apply(report)
        report["format"] = "donor-format"
        with self.assertRaisesRegex(
            inventory.InventoryError, "unsupported_inventory_format"
        ):
            self.apply(report)
        self.assertNotIn("secondary_indexes", self.db.references.find_one())

    def test_changed_retirement_prevents_preflight_writes_and_cannot_be_omitted(self):
        self.db.populations.insert_one(
            dict(
                _id="population", schema=self.schema, table_name="view_data", indexes=[]
            )
        )
        report = self.review()
        self.db.populations.update_one(
            {"_id": "population"}, {"$set": {"indexes": ["new"]}}
        )
        with self.assertRaisesRegex(
            inventory.InventoryError, "legacy_configuration_changed"
        ):
            self.apply(report)
        self.assertNotIn("secondary_indexes", self.db.references.find_one())
        report["legacy_retirements"][0]["approved"] = False
        with self.assertRaisesRegex(
            inventory.InventoryError, "all_population_legacy_retirements"
        ):
            self.apply(report)

    def test_imported_reference_needs_no_connector_but_keeps_explicit_destination(self):
        self.db.references.update_one(
            {"_id": "reference"},
            {"$set": {"is_imported_file": True}, "$unset": {"connector_id": ""}},
        )
        report = self.review()
        self.assertEqual(self.apply(report).applied, 1)
        self.assertEqual(self.db.references.find_one()["data_mart_schema"], self.schema)

    def test_cutover_retires_arrays_without_creating_missing_legacy_choice(self):
        legacy = [{"sql": f'CREATE INDEX missing ON "{self.schema}".data (extra)'}]
        self.db.populations.insert_one(
            dict(
                _id="population",
                schema=self.schema,
                table_name="view_data",
                query_indexes=legacy,
            )
        )
        report = self.review()
        self.assertEqual(self.apply(report).retired, 1)
        self.manager.register_legacy_population("population")
        self.assertEqual(self.db.populations.find_one()["query_indexes"], legacy)
        self.assertIsNone(inspect_index(self.cur, self.schema, "missing"))
        self.assertEqual(
            [
                i["index_name"]
                for i in self.db.references.find_one()["secondary_indexes"]
            ],
            ["manual"],
        )
        self.assertEqual(self.apply(report).retired, 0)

    def test_crash_after_first_intent_is_resumable_without_ddl_or_overwrite(self):
        self.cur.execute(f'CREATE INDEX second ON "{self.schema}".data (extra)')
        report = self.review()
        code = """import json, os, sys
import psycopg2
from pymongo import MongoClient
from ddp_lib import index_inventory as i
client = MongoClient(os.environ['QUEUE_TEST_MONGO_URI'])
conn = psycopg2.connect(os.environ['QUEUE_TEST_POSTGRES_DSN']); conn.autocommit = True
original = i.save_intent
def crash(*args, **kwargs):
    original(*args, **kwargs)
    os._exit(73)
i.save_intent = crash
i.apply_inventory(json.load(open(sys.argv[2])), client[sys.argv[1]], conn)
"""
        before = self.manual()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "reviewed.json"
            path.write_text(json.dumps(report))
            result = subprocess.run(
                [sys.executable, "-c", code, self.db.name, str(path)],
                capture_output=True,
                timeout=30,
            )
        self.assertEqual(result.returncode, 73, result.stderr.decode())
        entry = self.db.references.find_one()["secondary_indexes"][0]
        self.assertEqual(len(self.db.references.find_one()["secondary_indexes"]), 1)
        self.assertEqual(self.apply(report).applied, 1)
        self.assertEqual(self.db.references.find_one()["secondary_indexes"][0], entry)
        self.assertEqual(self.manual(), before)


if __name__ == "__main__":
    unittest.main()

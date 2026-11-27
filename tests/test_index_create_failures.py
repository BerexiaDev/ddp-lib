"""CREATE requests that can never succeed, and reload snapshots after success.

Database cases use disposable PostgreSQL/Mongo (QUEUE_TEST_POSTGRES_DSN,
QUEUE_TEST_MONGO_URI); the inventory helper cases need no database.
"""
import os
import unittest
from types import SimpleNamespace
from uuid import uuid4

from ddp_lib.dataset_indexes import DatasetIndexes, _is_permanent
from ddp_lib.index_inventory import _reload_unfinished
from ddp_lib.postgres_indexes import inspect_index
from ddp_lib.secondary_indexes import IndexRepairError


class PermanentCodeTests(unittest.TestCase):
    def test_classification(self):
        for code in ("23505", "42703", "42883", "22P02"):
            self.assertTrue(_is_permanent(code), code)
        for code in (None, "", "42P01", "42P07", "42710", "08006", "55P03", "40001", "XX000"):
            self.assertFalse(_is_permanent(code), code)


class ReloadSnapshotTests(unittest.TestCase):
    def test_finished_verified_snapshot_is_not_an_unfinished_reload(self):
        doc = {"index_data_token": "t1", "index_repair_stage": "verified",
               "index_repair_pending": False, "index_reload": {"token": "t1"}}
        self.assertFalse(_reload_unfinished(doc))

    def test_unfinished_reloads_still_block_import(self):
        base = {"index_data_token": "t1", "index_repair_stage": "verified",
                "index_repair_pending": False, "index_reload": {"token": "t1"}}
        for change in ({"index_repair_stage": "writing"},
                       {"index_repair_pending": True},
                       {"index_reload": {"token": "older"}}):
            with self.subTest(change=change):
                self.assertTrue(_reload_unfinished(dict(base, **change)))

    def test_no_snapshot(self):
        self.assertFalse(_reload_unfinished({}))


@unittest.skipUnless(
    os.getenv("QUEUE_TEST_POSTGRES_DSN") and os.getenv("QUEUE_TEST_MONGO_URI"),
    "Set disposable database variables",
)
class CreateFailureDatabaseTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        from pymongo import MongoClient

        self.connect = lambda: psycopg2.connect(os.environ["QUEUE_TEST_POSTGRES_DSN"])
        self.connection = self.connect()
        self.connection.autocommit = True
        self.cursor = self.connection.cursor()
        self.schema = "kan20fix_" + uuid4().hex[:12]
        self.cursor.execute(f'CREATE SCHEMA "{self.schema}"')
        self.cursor.execute(f'CREATE TABLE "{self.schema}".data (id int, amount int)')
        # Duplicate amounts make a UNIQUE index on amount impossible.
        self.cursor.execute(
            f'INSERT INTO "{self.schema}".data SELECT g, g % 10 FROM generate_series(1, 50) g'
        )
        self.client = MongoClient(os.environ["QUEUE_TEST_MONGO_URI"])
        self.db = self.client["kan20fix_" + uuid4().hex]
        self.db.references.insert_one(
            {"_id": "reference", "data_mart_schema": self.schema, "table_name": "data",
             "source_indexes": []}
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

    def target(self):
        return self.manager.get("REFERENCE", "reference")

    def entries(self):
        return self.db.references.find_one({"_id": "reference"}).get("secondary_indexes", [])

    def physical(self, name):
        return inspect_index(self.cursor, self.schema, name)

    def create(self, sql):
        return self.manager.create(self.target(), sql, {"type": "rule", "id": "r1"})

    def test_unknown_column_is_rejected_before_any_intent(self):
        with self.assertRaisesRegex(ValueError, "Invalid index definition"):
            self.create(f'CREATE INDEX bad_col ON "{self.schema}".data (no_such_col)')
        self.assertEqual(self.entries(), [])
        self.assertIsNone(self.physical("bad_col"))

    def test_unique_on_duplicates_is_withdrawn_and_leaves_nothing_behind(self):
        with self.assertRaisesRegex(ValueError, "could not be built"):
            self.create(f'CREATE UNIQUE INDEX CONCURRENTLY dup_amount ON "{self.schema}".data (amount)')
        [entry] = self.entries()
        self.assertEqual((entry["desired_state"], entry["status"]), ("absent", "deleted"))
        self.assertIsNone(self.physical("dup_amount"))  # no invalid leftover
        doc = self.db.references.find_one({"_id": "reference"})
        self.assertFalse(doc.get("index_repair_pending"))
        # The dataset is healthy again: another index can be created normally.
        self.create(f'CREATE INDEX CONCURRENTLY ok_amount ON "{self.schema}".data (amount)')
        self.assertTrue(self.physical("ok_amount").valid)

    def test_valid_request_still_creates_the_index(self):
        result = self.create(f'CREATE INDEX CONCURRENTLY ok_id ON "{self.schema}".data (id)')
        self.assertTrue(result["ready"])
        self.assertTrue(self.physical("ok_id").valid)

    def test_missing_table_keeps_the_intent_for_a_later_refresh(self):
        self.cursor.execute(f'DROP TABLE "{self.schema}".data')
        with self.assertRaises(IndexRepairError):
            self.create(f'CREATE INDEX later_idx ON "{self.schema}".data (id)')
        [entry] = self.entries()
        self.assertEqual(entry["desired_state"], "present")

    def test_withdrawing_an_unadopted_import_keeps_the_index_unmanaged(self):
        from ddp_lib.index_metadata import save_intent

        self.cursor.execute(f'CREATE INDEX dba_idx ON "{self.schema}".data (amount)')
        # What the I06 import writes: an adopt request, no DDL, no ownership stamp yet.
        save_intent(self.db.references, "reference", self.schema, "data",
                    f'CREATE INDEX dba_idx ON "{self.schema}".data (amount)',
                    {"type": "inventory"}, True)
        self.manager.delete(self.target(), "dba_idx")  # must not raise
        [entry] = self.entries()
        self.assertEqual((entry["desired_state"], entry["status"]), ("absent", "released"))
        physical = self.physical("dba_idx")
        self.assertIsNotNone(physical)  # left exactly as before the import
        self.assertNotIn("ddp-index", physical.comment or "")
        self.assertFalse(self.db.references.find_one({"_id": "reference"}).get("index_repair_pending"))
        # Later reconciles (every refresh runs one) keep ignoring it.
        for _ in range(2):
            self.assertTrue(self.manager.reconcile(self.target()).ready)
        self.assertIsNotNone(self.physical("dba_idx"))
        # If the index is later dropped by hand, the entry ends as a normal deletion.
        self.cursor.execute(f'DROP INDEX "{self.schema}".dba_idx')
        self.manager.reconcile(self.target())
        [entry] = self.entries()
        self.assertEqual((entry["status"], entry.get("adopt")), ("deleted", False))

    def test_an_adopted_and_stamped_index_is_still_dropped(self):
        self.cursor.execute(f'CREATE INDEX dba_idx ON "{self.schema}".data (amount)')
        self.manager.create(self.target(), f'CREATE INDEX dba_idx ON "{self.schema}".data (amount)',
                            {"type": "rule", "id": "r1"}, adopt=True)
        self.assertIn("ddp-index", self.physical("dba_idx").comment or "")
        self.manager.delete(self.target(), "dba_idx")
        self.assertIsNone(self.physical("dba_idx"))

    def test_verified_reconcile_clears_the_reload_snapshot(self):
        self.create(f'CREATE INDEX CONCURRENTLY ok_id ON "{self.schema}".data (id)')
        self.db.references.update_one(
            {"_id": "reference"}, {"$set": {"index_reload": {"token": "old"}}}
        )
        self.manager.reconcile(self.target())
        self.assertNotIn("index_reload", self.db.references.find_one({"_id": "reference"}))


if __name__ == "__main__":
    unittest.main()

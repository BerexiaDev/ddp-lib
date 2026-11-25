"""Canonical preparation, ownership and rollback on disposable databases."""

from contextlib import contextmanager
import os
import unittest
from unittest.mock import patch
from types import SimpleNamespace

import test_index_creation_modes as modes
from ddp_lib import secondary_indexes as lifecycle
from ddp_lib.index_metadata import begin_data, save_intent, IndexConflict
from ddp_lib.postgres_indexes import inspect_index


@unittest.skipUnless(
    os.getenv("QUEUE_TEST_POSTGRES_DSN") and os.getenv("QUEUE_TEST_MONGO_URI"),
    "Set PostgreSQL and MongoDB test connection variables",
)
class ReloadTests(unittest.TestCase):
    setUp = modes.CreationModeTests.setUp
    tearDown = modes.CreationModeTests.tearDown
    sql = modes.CreationModeTests.sql
    save = modes.CreationModeTests.save
    physical = modes.CreationModeTests.physical

    def prepare(self, target=None):
        target = target or self.target
        begin_data(self.collection, target.id, "attempt")
        connection = self.connect()
        try:
            result = self.manager.prepare_reload(target, connection, "attempt")
            connection.commit()
            return result
        finally:
            connection.close()

    def test_constraint_primary_and_unmanaged_indexes_keep_oids(self):
        self.cursor.execute(f'ALTER TABLE "{self.schema}".data ADD PRIMARY KEY (id)')
        self.cursor.execute(
            f'ALTER TABLE "{self.schema}".data ADD CONSTRAINT uq UNIQUE(value)'
        )
        self.cursor.execute(
            f'CREATE INDEX unmanaged ON "{self.schema}".data (id, value)'
        )
        for name, column in [("data_pkey", "id"), ("uq", "value")]:
            save_intent(
                self.collection,
                "target",
                self.schema,
                "data",
                f'CREATE UNIQUE INDEX {name} ON "{self.schema}".data ({column})',
            )
        before = {
            n: inspect_index(self.cursor, self.schema, n).oid
            for n in ("data_pkey", "uq", "unmanaged")
        }
        self.assertEqual(self.prepare().dropped, ())
        self.assertEqual(
            {n: inspect_index(self.cursor, self.schema, n).oid for n in before}, before
        )

    def test_partition_parent_and_attached_child_never_dropped(self):
        self.cursor.execute(f'DROP TABLE "{self.schema}".data')
        self.cursor.execute(
            f'CREATE TABLE "{self.schema}".data(id int, value text) PARTITION BY RANGE(id)'
        )
        self.cursor.execute(
            f'CREATE TABLE "{self.schema}".leaf PARTITION OF "{self.schema}".data FOR VALUES FROM (0) TO (10)'
        )
        self.manager.create(self.target, self.sql(), {})
        parent = self.physical()
        self.cursor.execute(
            "SELECT c.relname FROM pg_inherits h JOIN pg_class c ON c.oid=h.inhrelid WHERE h.inhparent=%s",
            (parent.oid,),
        )
        name = self.cursor.fetchone()[0]
        child = inspect_index(self.cursor, self.schema, name)
        self.assertEqual(self.prepare().dropped, ())
        self.collection.insert_one(
            {"_id": "leaf", "data_mart_schema": self.schema, "table_name": "leaf"}
        )
        save_intent(
            self.collection, "leaf", self.schema, "leaf", child.definition, adopt=True
        )
        self.assertEqual(
            self.prepare(self.manager.get("REFERENCE", "leaf")).dropped, ()
        )
        self.assertEqual(self.physical().oid, parent.oid)
        self.assertEqual(inspect_index(self.cursor, self.schema, name).oid, child.oid)

    def test_revision_cas_failure_prevents_drop(self):
        from pymongo.collection import Collection

        self.save()
        self.manager.reconcile(self.target)
        before = self.physical().oid
        original = Collection.update_one

        def race(collection, query, update, *args, **kwargs):
            if "index_reload" in update.get("$set", {}):
                return SimpleNamespace(matched_count=0)
            return original(collection, query, update, *args, **kwargs)

        self.events.clear()
        with (
            patch.object(Collection, "update_one", new=race),
            self.assertRaisesRegex(IndexConflict, "definitions changed"),
        ):
            self.prepare()
        self.assertEqual(self.physical().oid, before)
        self.assertFalse(any(sql.startswith("DROP INDEX") for sql, _ in self.events))

    def test_late_reconciliation_error_does_not_mark_newer_data_broken(self):
        self.save()
        begin_data(self.collection, "target", "attempt-A")
        original = lifecycle.dataset_session

        @contextmanager
        def delayed(*args, **kwargs):
            with original(*args, **kwargs) as connection:
                yield connection
            # New ownership starts after A releases the dataset lock.
            self.collection.update_one(
                {"_id": "target"},
                {
                    "$set": {
                        "index_data_token": "attempt-B",
                        "index_data_ready": False,
                        "index_repair_error": None,
                        "index_repair_pending": False,
                    }
                },
            )
            raise RuntimeError("delayed worker A error")

        with (
            patch.object(lifecycle, "dataset_session", delayed),
            self.assertRaises(lifecycle.IndexRepairError),
        ):
            lifecycle.reconcile(
                self.collection, "target", self.schema, "data", self.connect
            )
        current = self.collection.find_one()
        self.assertEqual(current["index_data_token"], "attempt-B")
        self.assertIsNone(current["index_repair_error"])
        self.assertFalse(current["index_repair_pending"])

    def test_temporary_removal_rejects_autocommit_and_wrong_token(self):
        with self.assertRaisesRegex(ValueError, "publication transaction"):
            self.manager.prepare_reload(self.target, self.connection, "attempt")
        connection = self.connect()
        try:
            begin_data(self.collection, "target", "newer")
            with self.assertRaises(IndexConflict):
                self.manager.prepare_reload(self.target, connection, "old")
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()

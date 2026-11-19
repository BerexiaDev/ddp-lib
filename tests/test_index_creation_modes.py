"""I04 mode, timeout and checkpoint contracts on disposable PostgreSQL/MongoDB."""
from contextlib import contextmanager
import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from ddp_lib.dataset_indexes import DatasetIndexes
from ddp_lib.enums import IndexCreationMode
from ddp_lib.index_metadata import begin_data, data_committed, save_intent
from ddp_lib.postgres_indexes import inspect_index
from ddp_lib.secondary_indexes import IndexRepairError, owns


@unittest.skipUnless(os.getenv('QUEUE_TEST_POSTGRES_DSN') and os.getenv('QUEUE_TEST_MONGO_URI'),
                     'Set QUEUE_TEST_POSTGRES_DSN and QUEUE_TEST_MONGO_URI to disposable databases')
class CreationModeTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        from psycopg2 import extensions
        from pymongo import MongoClient
        self.events = []
        test = self

        class Cursor(extensions.cursor):
            def execute(self, statement, parameters=None):
                sql = statement if isinstance(statement, str) else statement.as_string(self)
                test.events.append((sql, parameters))
                return super().execute(statement, parameters)

        self.connect = lambda: psycopg2.connect(os.environ['QUEUE_TEST_POSTGRES_DSN'], cursor_factory=Cursor)
        self.connection = self.connect()
        self.connection.autocommit = True
        self.cursor = self.connection.cursor()
        self.schema = 'i04_' + uuid4().hex[:12]
        self.cursor.execute(f'CREATE SCHEMA "{self.schema}"')
        self.cursor.execute(f'CREATE TABLE "{self.schema}".data (id int, value text)')
        self.cursor.execute(f'INSERT INTO "{self.schema}".data VALUES (1, \'committed\')')
        self.cursor.execute(f'CREATE TABLE "{self.schema}".unrelated (id int)')
        self.client = MongoClient(os.environ['QUEUE_TEST_MONGO_URI'])
        self.db = self.client['i04_' + uuid4().hex]
        self.collection = self.db.references
        self.collection.insert_one({'_id': 'target', 'data_mart_schema': self.schema, 'table_name': 'data'})
        self.manager = DatasetIndexes(self.db, lambda schema: SimpleNamespace(get_connection=self.connect),
                                      lambda doc: doc['data_mart_schema'])
        self.target = self.manager.get('REFERENCE', 'target')
        timeout = patch.dict(os.environ, INDEX_LOCK_TIMEOUT='200ms', INDEX_STATEMENT_TIMEOUT='3s')
        timeout.start()
        self.addCleanup(timeout.stop)

    def tearDown(self):
        self.cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.cursor.close()
        self.connection.close()
        self.client.drop_database(self.db.name)
        self.client.close()

    def sql(self, name='idx'):
        return f'CREATE INDEX CONCURRENTLY "{name}" ON "{self.schema}".data ((lower(value)))'

    def save(self):
        return save_intent(self.collection, 'target', self.schema, 'data', self.sql())

    def checkpoint(self):
        begin_data(self.collection, 'target', 'committed-attempt')
        data_committed(self.collection, 'target', 'committed-attempt')

    def physical(self):
        return inspect_index(self.cursor, self.schema, 'idx')

    @contextmanager
    def old_snapshot(self):
        reader = self.connect()
        try:
            reader.set_session(isolation_level='REPEATABLE READ', readonly=True)
            with reader.cursor() as cursor:
                cursor.execute(f'SELECT * FROM "{self.schema}".unrelated')
                cursor.fetchall()
            yield reader
        finally:
            reader.close()

    def creates(self):
        return [sql for sql, _ in self.events if sql.startswith('CREATE INDEX')
                and f'ON "{self.schema}"."data"' in sql]

    def test_I04_A_post_load_restores_source_and_secondary_under_old_snapshot(self):
        self.save()
        self.collection.update_one({'_id': 'target'}, {'$set': {'source_indexes': [{'columns': ['id']}]}})
        self.checkpoint()
        with self.old_snapshot():
            result = self.manager.reconcile(self.target, mode=IndexCreationMode.POST_LOAD.value)
            self.assertTrue(result.ready)
            self.assertEqual(result.applied, 2)
            self.assertTrue(self.physical().valid and self.physical().ready)
        self.assertEqual(len(self.creates()), 2)
        self.assertTrue(all('CONCURRENTLY' not in sql for sql in self.creates()))
        self.assertFalse(self.collection.find_one()['index_repair_pending'])

    def test_I04_B_owned_invalid_build_repairs_committed_data_under_old_snapshot(self):
        entry = self.save()
        self.checkpoint()
        self.cursor.execute(f'SELECT ctid::text, * FROM "{self.schema}".data')
        before = self.cursor.fetchall()
        with self.old_snapshot():
            # Positive control: online CREATE really waits on this unrelated
            # snapshot, times out, and leaves an invalid index with a receipt.
            with self.assertRaises(IndexRepairError):
                self.manager.reconcile(self.target)
            invalid = self.physical()
            self.assertFalse(invalid.valid)
            self.assertTrue(invalid.ready)
            self.assertTrue(owns(entry, invalid))
            self.events.clear()
            self.assertTrue(self.manager.repair(self.target).ready)
            self.assertTrue(self.physical().valid and self.physical().ready)
            self.assertNotEqual(invalid.oid, self.physical().oid)
        self.assertEqual(len(self.creates()), 1)
        self.assertNotIn('CONCURRENTLY', self.creates()[0])
        self.assertTrue(any(sql.startswith('DROP INDEX CONCURRENTLY') for sql, _ in self.events))
        self.cursor.execute(f'SELECT ctid::text, * FROM "{self.schema}".data')
        self.assertEqual(self.cursor.fetchall(), before)
        self.assertEqual(self.collection.find_one()['index_data_token'], 'committed-attempt')

    def test_I04_C_online_requests_remain_concurrent_even_with_old_checkpoint(self):
        self.checkpoint()
        self.manager.create(self.target, self.sql().replace('CONCURRENTLY ', ''), {})
        self.assertEqual(len(self.creates()), 1)
        self.assertIn('CONCURRENTLY', self.creates()[0])
        self.cursor.execute(f'CREATE MATERIALIZED VIEW "{self.schema}".view_data AS SELECT * FROM "{self.schema}".data')
        self.db.populations.insert_one({'_id': 'view', 'schema': self.schema, 'table_name': 'view_data'})
        target = self.manager.get('POPULATION', 'view')
        sql = f'CREATE INDEX view_idx ON "{self.schema}".view_data (id)'
        self.manager.create(target, sql, {})
        self.assertTrue(any(s.startswith('CREATE INDEX CONCURRENTLY "view_idx"') for s, _ in self.events))

    def test_I04_D_partition_parent_uses_ordinary_ddl_and_attached_children(self):
        self.cursor.execute(f'DROP TABLE "{self.schema}".data')
        self.cursor.execute(f'CREATE TABLE "{self.schema}".data(id int, value text) PARTITION BY RANGE(id)')
        for name, start, end in [('part_a', 0, 10), ('part_b', 10, 20)]:
            self.cursor.execute(f'CREATE TABLE "{self.schema}".{name} PARTITION OF "{self.schema}".data FOR VALUES FROM ({start}) TO ({end})')
        self.manager.create(self.target, self.sql(), {})
        self.assertNotIn('CONCURRENTLY', self.creates()[0])
        self.assertTrue(self.physical().valid and self.physical().ready)
        self.cursor.execute('''SELECT count(*) FROM pg_inherits h JOIN pg_index i ON i.indexrelid=h.inhrelid
                               WHERE h.inhparent=%s AND i.indisvalid AND i.indisready''', (self.physical().oid,))
        self.assertEqual(self.cursor.fetchone()[0], 2)

    def test_I04_E_target_writer_timeouts_preserve_intent_and_allow_retry(self):
        entry = self.save()
        self.checkpoint()
        writer = self.connect()
        try:
            with writer.cursor() as cursor:
                cursor.execute(f'INSERT INTO "{self.schema}".data VALUES (2, \'uncommitted\')')
            for lock, statement, message in [('150ms', '3s', 'lock timeout'), ('3s', '150ms', 'statement timeout')]:
                with self.subTest(timeout=message), patch.dict(os.environ,
                        INDEX_LOCK_TIMEOUT=lock, INDEX_STATEMENT_TIMEOUT=statement):
                    started = time.monotonic()
                    with self.assertRaisesRegex(IndexRepairError, message) as caught:
                        self.manager.reconcile(self.target, mode=IndexCreationMode.POST_LOAD.value)
                    self.assertLess(time.monotonic() - started, 2)
                    self.assertFalse(caught.exception.result.ready)
                    document = self.collection.find_one()
                    current = document['secondary_indexes'][0]
                    self.assertEqual((current['id'], current['revision'], current['desired_state']),
                                     (entry['id'], entry['revision'], 'present'))
                    self.assertEqual(current['status'], 'error')
                    self.assertTrue(document['index_repair_pending'] and document['index_data_ready'])
                    self.assertFalse(document['indexes_applied'])
                    self.assertIsNone(self.physical())
        finally:
            writer.close()
        self.assertTrue(self.manager.repair(self.target).ready)
        self.assertFalse(self.collection.find_one()['index_repair_pending'])

    def test_manual_repair_cannot_invent_or_use_an_uncommitted_checkpoint(self):
        self.save()
        with self.assertRaisesRegex(IndexRepairError, 'committed data checkpoint'):
            self.manager.reconcile(self.target, mode=IndexCreationMode.POST_LOAD.value)
        self.assertFalse(self.creates())
        self.assertTrue(self.manager.repair(self.target).ready)
        self.assertIn('CONCURRENTLY', self.creates()[0])
        self.assertNotIn('index_data_ready', self.collection.find_one())
        self.checkpoint()
        stale_target = self.manager.get('REFERENCE', 'target')
        begin_data(self.collection, 'target', 'new-attempt')
        self.events.clear()
        with self.assertRaisesRegex(IndexRepairError, 'has not committed'):
            self.manager.repair(stale_target)
        with self.assertRaisesRegex(IndexRepairError, 'committed data checkpoint'):
            self.manager.reconcile(stale_target, mode=IndexCreationMode.POST_LOAD.value)
        self.assertFalse(self.creates())
        self.assertFalse(self.collection.find_one()['index_data_ready'])

    def test_timeouts_apply_on_each_borrowed_connection_before_any_ddl(self):
        self.save()
        with patch.dict(os.environ):
            os.environ.pop('INDEX_LOCK_TIMEOUT', None)
            os.environ.pop('INDEX_STATEMENT_TIMEOUT', None)
            for mode, lock, statement in [
                (IndexCreationMode.ONLINE.value, '5s', '30min'),
                (IndexCreationMode.POST_LOAD.value, '700ms', '4s'),
            ]:
                if mode == IndexCreationMode.POST_LOAD.value:
                    self.checkpoint()
                    os.environ.update(INDEX_LOCK_TIMEOUT=lock, INDEX_STATEMENT_TIMEOUT=statement)
                    self.cursor.execute(f'DROP INDEX "{self.schema}".idx')
                self.events.clear()
                self.manager.reconcile(self.target, self.connection, mode=mode)
                lock_event = ('SET lock_timeout = %s', (lock,))
                statement_event = ('SET statement_timeout = %s', (statement,))
                first_ddl = next(i for i, (sql, _) in enumerate(self.events) if sql.startswith('CREATE '))
                self.assertLess(self.events.index(lock_event), first_ddl)
                self.assertLess(self.events.index(statement_event), first_ddl)
                self.assertFalse(self.connection.closed)

    def test_borrowed_connection_rechecks_checkpoint_after_acquiring_guard(self):
        self.save()
        self.checkpoint()
        original = self.manager.guard

        @contextmanager
        def newer_attempt(target, connection=None):
            with original(target, connection) as guarded:
                begin_data(self.collection, 'target', 'new-attempt')
                yield guarded

        with patch.object(self.manager, 'guard', side_effect=newer_attempt):
            with self.assertRaisesRegex(IndexRepairError, 'committed data checkpoint'):
                self.manager.reconcile(self.target, self.connection, mode=IndexCreationMode.POST_LOAD.value)
        self.assertFalse(self.creates())
        self.assertFalse(self.connection.closed)
        self.assertFalse(self.collection.find_one()['index_data_ready'])


if __name__ == '__main__':
    unittest.main()

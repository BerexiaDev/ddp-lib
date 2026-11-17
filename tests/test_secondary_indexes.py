"""I01 contracts against disposable PostgreSQL/MongoDB, not donor mocks."""
import os
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from ddp_lib.index_sql import parse_index
from ddp_lib.index_metadata import (IndexConflict, save_intent, delete_intent, record_result,
    editable_fields, begin_data, data_committed)


class ParserTests(unittest.TestCase):
    def test_I01_E_rejects_multiple_wrong_grammar_and_truncation(self):
        for statement in [
            'CREATE INDEX i ON s.t (id); DROP TABLE s.t', 'DROP INDEX s.i',
            'CREATE INDEX i ON t (id)', 'CREATE INDEX i ON s.t',
            'CREATE INDEX ' + 'a'*64 + ' ON s.t (id)',
            'CREATE INDEX "' + 'é'*32 + '" ON s.t (id)',
            'CREATE INDEX i ON s.t ("' + 'a'*64 + '")',
        ]:
            with self.subTest(statement=statement), self.assertRaises(ValueError):
                parse_index(statement)

    def test_I01_F_preserves_literals_expressions_order_and_only(self):
        sql = 'CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS "Name" ON ONLY "My Schema"."Table" ((lower("Name")), id DESC) INCLUDE (value) WHERE value = \'a  b\''
        definition = parse_index(sql)
        replay = definition.render('new schema', 'renamed')
        self.assertIn("'a  b'", replay)
        self.assertIn('lower("Name")', replay)
        self.assertIn('ON ONLY "new schema"."renamed"', replay)
        self.assertEqual(definition.signature, parse_index(replay).signature)
        self.assertNotEqual(definition.signature, parse_index(sql.replace('a  b', 'a b')).signature)

    def test_I01_C_protected_dotted_paths(self):
        fields = {'_id': 'evil', 'secondary_indexes.0.sql': 'evil', 'index_data_ready': True,
                  'index_data_token.x': 'evil', 'index_repair_error': None, 'name': 'new'}
        self.assertEqual(editable_fields('references', fields), {'name': 'new'})


@unittest.skipUnless(os.getenv('QUEUE_TEST_POSTGRES_DSN') and os.getenv('QUEUE_TEST_MONGO_URI'),
                     'Set QUEUE_TEST_POSTGRES_DSN and QUEUE_TEST_MONGO_URI to disposable databases')
class LifecycleTests(unittest.TestCase):
    def setUp(self):
        import psycopg2
        from pymongo import MongoClient
        from ddp_lib import secondary_indexes
        self.life = secondary_indexes
        self.client = MongoClient(os.environ['QUEUE_TEST_MONGO_URI'])
        self.db = self.client['kan20_' + uuid4().hex]
        self.collection = self.db.populations
        self.schema = 'kan20_' + uuid4().hex[:12]
        self.collection.insert_one({'_id': 'target', 'schema': self.schema, 'table_name': 'data'})
        self.connect = lambda: psycopg2.connect(os.environ['QUEUE_TEST_POSTGRES_DSN'])
        self.connection = self.connect()
        self.connection.autocommit = True
        self.cursor = self.connection.cursor()
        self.cursor.execute(f'CREATE SCHEMA "{self.schema}"')
        self.cursor.execute(f'CREATE TABLE "{self.schema}".data (id int, value text, "Name" text)')

    def tearDown(self):
        self.cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.cursor.close()
        self.connection.close()
        self.client.drop_database(self.db.name)
        self.client.close()

    def sql(self, name='idx', body='(id)', unique=False):
        return f'CREATE {"UNIQUE " if unique else ""}INDEX CONCURRENTLY "{name}" ON "{self.schema}".data {body}'

    def save(self, sql=None, **options):
        return save_intent(self.collection, 'target', self.schema, 'data', sql or self.sql(), **options)

    def run_indexes(self):
        return self.life.reconcile(self.collection, 'target', self.schema, 'data', self.connect)

    def physical(self, name='idx'):
        from ddp_lib.postgres_indexes import inspect_index
        return inspect_index(self.cursor, self.schema, name)

    def test_I01_A_B_restore_definition_and_keep_tombstone_across_two_rebuilds(self):
        entry = self.save(self.sql(body='((lower("Name")), id DESC) INCLUDE (value) WHERE value = \'a  b\''))
        self.run_indexes()
        original = self.physical().definition
        for _ in range(2):
            self.cursor.execute(f'DROP TABLE "{self.schema}".data')
            self.cursor.execute(f'CREATE TABLE "{self.schema}".data (id int, value text, "Name" text)')
            self.run_indexes()
            self.assertEqual(self.physical().definition, original)
            self.assertTrue(self.physical().valid and self.physical().ready)
        delete_intent(self.collection, 'target', self.schema, 'idx')
        self.run_indexes()
        for _ in range(2):
            self.cursor.execute(f'DROP TABLE "{self.schema}".data')
            self.cursor.execute(f'CREATE TABLE "{self.schema}".data (id int, value text, "Name" text)')
            self.run_indexes()
            self.assertIsNone(self.physical())
        tombstone = self.collection.find_one()['secondary_indexes'][0]
        self.assertEqual((tombstone['id'], tombstone['desired_state'], tombstone['revision']), (entry['id'], 'absent', 2))

    def test_I01_D_result_revision_and_legacy_counter_are_fenced(self):
        created = self.save()
        deleted = delete_intent(self.collection, 'target', self.schema, 'idx')
        self.assertFalse(record_result(self.collection, 'target', created['id'], created['revision'], {'status': 'created'}))
        self.assertEqual(self.collection.find_one()['secondary_indexes'][0]['revision'], deleted['revision'])
        recreated = self.save()
        self.assertEqual(recreated['revision'], 3)
        self.assertEqual(self.save()['revision'], 3)

    def test_I01_D_concurrent_requests_and_mutation_during_ddl(self):
        barrier = threading.Barrier(8)
        errors = []
        def create(i):
            try:
                barrier.wait()
                self.save(self.sql(name='idx' + str(i)))
            except Exception as exc:
                errors.append(exc)
        threads = [threading.Thread(target=create, args=(i,)) for i in range(8)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertFalse(errors)
        self.assertEqual(len(self.collection.find_one()['secondary_indexes']), 8)
        original = self.life.apply_entry
        def delayed(cursor, entry, *args, **kwargs):
            result = original(cursor, entry, *args, **kwargs)
            delete_intent(self.collection, 'target', self.schema, entry['index_name'])
            return result
        with patch.object(self.life, 'apply_entry', side_effect=delayed), self.assertRaises(self.life.IndexRepairError):
            self.run_indexes()
        self.assertTrue(all(e['desired_state'] == 'absent' for e in self.collection.find_one()['secondary_indexes']))
        self.run_indexes()
        self.assertIsNone(self.physical('idx0'))

    def test_I01_E_wrong_target_and_definition_conflict_have_no_ddl(self):
        with self.assertRaises(ValueError):
            self.save('CREATE INDEX idx ON wrong.table (id)')
        self.save()
        with self.assertRaises(IndexConflict):
            self.save(self.sql(body='(value)'))
        self.assertIsNone(self.physical())

    def test_I01_G_unmanaged_constraint_and_other_table_are_not_taken_over(self):
        self.cursor.execute(self.sql())
        oid = self.physical().oid
        self.save()
        with self.assertRaises(self.life.IndexRepairError): self.run_indexes()
        self.assertEqual(self.physical().oid, oid)
        delete_intent(self.collection, 'target', self.schema, 'idx')
        with self.assertRaises(self.life.IndexRepairError): self.run_indexes()
        self.assertEqual(self.physical().oid, oid)

    def test_saved_constraints_do_not_block_other_indexes_or_get_dropped(self):
        self.cursor.execute(f'ALTER TABLE "{self.schema}".data ADD CONSTRAINT primary_idx PRIMARY KEY (id)')
        self.cursor.execute(f'ALTER TABLE "{self.schema}".data ADD CONSTRAINT unique_idx UNIQUE (value)')
        oids = {name: self.physical(name).oid for name in ('primary_idx', 'unique_idx')}
        for name in oids:
            self.save(self.sql(name, unique=True), adopt=True)
        self.save()
        self.assertTrue(self.run_indexes().ready)
        self.assertTrue(self.physical().valid)
        entries = {e['index_name']: e for e in self.collection.find_one()['secondary_indexes']}
        for name, oid in oids.items():
            self.assertEqual(entries[name]['status'], 'error')
            self.assertIn(name, entries[name]['last_error'])
            self.assertEqual(self.physical(name).oid, oid)
            delete_intent(self.collection, 'target', self.schema, name)
        self.assertTrue(self.run_indexes().ready)
        for name, oid in oids.items():
            self.assertEqual(self.physical(name).oid, oid)
            self.assertTrue(self.physical(name).constraint_owned)

    def test_create_rejects_constraint_names_before_saving(self):
        from ddp_lib.dataset_indexes import DatasetIndexes
        from types import SimpleNamespace
        self.cursor.execute(f'ALTER TABLE "{self.schema}".data ADD CONSTRAINT primary_idx PRIMARY KEY (id)')
        self.cursor.execute(f'ALTER TABLE "{self.schema}".data ADD CONSTRAINT unique_idx UNIQUE (value)')
        manager = DatasetIndexes(self.db, lambda schema: SimpleNamespace(get_connection=self.connect),
                                 lambda doc: doc['data_mart_schema'])
        target = manager.get('POPULATION', 'target')
        before = self.collection.find_one()
        for name in ('primary_idx', 'unique_idx'):
            oid = self.physical(name).oid
            with self.assertRaisesRegex(IndexConflict, name):
                manager.create(target, self.sql(name, unique=True), {})
            self.assertEqual(self.collection.find_one(), before)
            self.assertEqual(self.physical(name).oid, oid)

    def test_other_table_constraint_remains_a_conflict(self):
        self.cursor.execute(f'CREATE TABLE "{self.schema}".other (id int CONSTRAINT other_pkey PRIMARY KEY)')
        oid = self.physical('other_pkey').oid
        self.save(self.sql('other_pkey'))
        for state in ('present', 'absent'):
            if state == 'absent':
                delete_intent(self.collection, 'target', self.schema, 'other_pkey')
            with self.assertRaisesRegex(self.life.IndexRepairError,
                                        'other_pkey: Index name is occupied by another relation'):
                self.run_indexes()
            self.assertEqual(self.physical('other_pkey').oid, oid)

    def test_source_constraint_still_fails_reconciliation(self):
        self.cursor.execute(f'ALTER TABLE "{self.schema}".data ADD CONSTRAINT primary_idx PRIMARY KEY (id)')
        oid = self.physical('primary_idx').oid
        source = {'id': 'source:test', 'index_name': 'primary_idx',
                  'sql': self.sql('primary_idx', unique=True), 'desired_state': 'present',
                  'revision': 1, 'adopt': False}
        with self.assertRaisesRegex(self.life.IndexRepairError, 'primary_idx'):
            self.life.reconcile(self.collection, 'target', self.schema, 'data', self.connect,
                                source_entries=[source])
        self.assertEqual(self.physical('primary_idx').oid, oid)
        self.assertTrue(self.collection.find_one()['index_repair_pending'])

    def test_I01_G_explicit_adoption_verifies_and_preserves_comment(self):
        self.cursor.execute(self.sql())
        self.cursor.execute(f'COMMENT ON INDEX "{self.schema}".idx IS %s', ('organization:{"owner":"ops"}',))
        self.save(adopt=True)
        self.run_indexes()
        self.assertTrue(self.physical().comment.startswith('organization:{"owner":"ops"}\n'))
        self.assertFalse(self.collection.find_one()['secondary_indexes'][0]['adopt'])
        self.cursor.execute(f'DROP INDEX "{self.schema}".idx')
        self.cursor.execute(self.sql())
        with self.assertRaises(self.life.IndexRepairError): self.run_indexes()

    def test_I01_H_receipt_recovers_metadata_outage_but_missing_receipt_requires_review(self):
        self.save()
        with patch.object(self.life, 'record_result', side_effect=RuntimeError('Mongo unavailable')):
            with self.assertRaises(self.life.IndexRepairError): self.run_indexes()
        self.assertIn('[ddp-index:v1:', self.physical().comment)
        self.run_indexes()
        self.assertEqual(self.collection.find_one()['secondary_indexes'][0]['status'], 'created')
        self.cursor.execute(f'COMMENT ON INDEX "{self.schema}".idx IS NULL')
        with self.assertRaises(self.life.IndexRepairError): self.run_indexes()

    def test_I01_H_failed_unique_build_gets_receipt_and_repairs(self):
        self.cursor.execute(f'INSERT INTO "{self.schema}".data(id) VALUES(1),(1)')
        self.save(self.sql(unique=True))
        with self.assertRaises(self.life.IndexRepairError): self.run_indexes()
        self.assertFalse(self.physical().valid)
        self.assertIn('[ddp-index:v1:', self.physical().comment)
        self.cursor.execute(f'DELETE FROM "{self.schema}".data WHERE ctid IN (SELECT ctid FROM "{self.schema}".data LIMIT 1)')
        self.run_indexes()
        self.assertTrue(self.physical().valid)

    def test_I01_I_dataset_and_name_contention_close_connections(self):
        from ddp_lib.postgres_indexes import advisory_lock
        self.save()
        for key in [f'ddp-index:v1:dataset:{self.collection.full_name}:target', f'ddp-index:v1:name:{self.schema}:idx']:
            opened = []
            def connect():
                conn = self.connect(); opened.append(conn); return conn
            with advisory_lock(self.connection, key):
                with self.assertRaises(self.life.IndexRepairError):
                    self.life.reconcile(self.collection, 'target', self.schema, 'data', connect)
            self.assertTrue(opened and all(c.closed for c in opened))
        self.run_indexes()

    def test_supplied_ddl_connection_obeys_dataset_lock_and_is_not_closed(self):
        from ddp_lib.postgres_indexes import advisory_lock, IndexLockBusy
        self.save()
        borrowed = self.connect()
        try:
            with self.assertRaises(ValueError):
                self.life.reconcile(self.collection, 'target', self.schema, 'data', self.connect, borrowed)
            borrowed.autocommit = True
            with advisory_lock(self.connection, f'ddp-index:v1:dataset:{self.collection.full_name}:target'):
                with self.assertRaises(IndexLockBusy):
                    self.life.reconcile(self.collection, 'target', self.schema, 'data', self.connect, borrowed)
            self.assertFalse(borrowed.closed)
            self.life.reconcile(self.collection, 'target', self.schema, 'data', self.connect, borrowed)
            self.assertFalse(borrowed.closed)
            self.assertTrue(self.physical().valid)
        finally:
            borrowed.close()

    def test_checkpoint_and_completion_cas_do_not_claim_data_success(self):
        begin_data(self.collection, 'target', 'attempt')
        self.save()
        self.run_indexes()
        self.assertFalse(self.collection.find_one()['index_data_ready'])
        self.assertTrue(self.collection.find_one()['index_repair_pending'])
        with self.assertRaises(IndexConflict): data_committed(self.collection, 'target', 'old')
        data_committed(self.collection, 'target', 'attempt')
        self.run_indexes()
        self.assertFalse(self.collection.find_one()['index_repair_pending'])

    def test_I01_F_rebind_rename_without_rewriting_literal_or_expression(self):
        self.save(self.sql(body="((lower(value))) WHERE value = 'a  b'"))
        self.run_indexes()
        self.cursor.execute(f'ALTER TABLE "{self.schema}".data RENAME TO renamed')
        self.life.reconcile(self.collection, 'target', self.schema, 'renamed', self.connect)
        entry = self.collection.find_one()['secondary_indexes'][0]
        self.assertIn("'a  b'", entry['sql'])
        self.assertEqual(entry['table'], 'renamed')
        self.assertTrue(self.physical().valid)

    def test_unicode_identifiers_and_dollar_quoted_literal_are_replayed(self):
        self.save(self.sql('indice_é', "((value || $$two  spaces;$$))"))
        self.run_indexes()
        self.assertTrue(self.physical('indice_é').valid)
        self.assertIn('two  spaces;', self.physical('indice_é').definition)

    def test_invalid_partition_parent_requires_attachment_review(self):
        self.cursor.execute(f'DROP TABLE "{self.schema}".data')
        self.cursor.execute(f'CREATE TABLE "{self.schema}".data(id int) PARTITION BY RANGE(id)')
        self.cursor.execute(f'CREATE TABLE "{self.schema}".part PARTITION OF "{self.schema}".data FOR VALUES FROM(0) TO(10)')
        self.save(self.sql().replace('ON ', 'ON ONLY '))
        with self.assertRaises(self.life.IndexRepairError): self.run_indexes()
        oid = self.physical().oid
        with self.assertRaisesRegex(self.life.IndexRepairError, 'attachment review'): self.run_indexes()
        self.assertEqual(self.physical().oid, oid)

    def test_I01_G_source_policy_classification_and_secondary_delete_rejection(self):
        from ddp_lib.dataset_indexes import DatasetIndexes
        from types import SimpleNamespace
        self.collection.delete_many({})
        self.db.references.insert_one({'_id': 'ref', 'data_mart_schema': self.schema,
            'table_name': 'data', 'source_indexes': [{'columns': ['id']}]})
        self.cursor.execute(self.sql('historic_source'))
        manager = DatasetIndexes(self.db, lambda schema: SimpleNamespace(get_connection=self.connect),
                                 lambda doc: doc['data_mart_schema'])
        target = manager.get('REFERENCE', 'ref')
        self.assertEqual(manager.listing(target)[0]['classification'], 'source')
        with self.assertRaises(IndexConflict): manager.delete(target, 'historic_source')
        with self.assertRaises(IndexConflict): manager.create(target, self.sql('historic_source'), {})
        with self.assertRaises(self.life.IndexRepairError): manager.reconcile(target)
        self.assertIsNone(self.physical('historic_source').comment)

    def test_duplicate_physical_metadata_owners_block_lifecycle(self):
        from ddp_lib.dataset_indexes import DatasetIndexes
        self.db.references.insert_one({'_id':'other', 'data_mart_schema':self.schema, 'table_name':'data'})
        manager = DatasetIndexes(self.db, None, lambda doc: doc['data_mart_schema'])
        with self.assertRaisesRegex(IndexConflict, 'Duplicate'):
            manager.get('POPULATION', 'target')

    def test_completion_cas_cannot_clear_newer_request(self):
        self.save()
        real_update = self.collection.update_one
        inserted = []
        def racing_update(query, update, **kwargs):
            if update.get('$set', {}).get('index_repair_pending') is False and not inserted:
                inserted.append(True)
                self.save(self.sql('newer'))
            return real_update(query, update, **kwargs)
        with patch.object(self.collection, 'update_one', side_effect=racing_update):
            with self.assertRaises(self.life.IndexRepairError): self.run_indexes()
        self.assertTrue(self.collection.find_one()['index_repair_pending'])
        self.assertIsNone(self.physical('newer'))
        self.run_indexes()

    def test_legacy_import_does_not_recreate_a_newer_tombstone(self):
        entry = self.save()
        delete_intent(self.collection, 'target', self.schema, 'idx')
        result = save_intent(self.collection, 'target', self.schema, 'data', self.sql(), only_if_missing=True)
        self.assertEqual(result['desired_state'], 'absent')
        self.assertEqual(result['id'], entry['id'])


if __name__ == '__main__':
    unittest.main()

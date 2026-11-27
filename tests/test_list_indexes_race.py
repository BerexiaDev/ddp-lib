"""list_indexes while a concurrent DROP changes the catalog.

The fake-cursor cases need no database; the last class uses disposable PostgreSQL.
"""
import os
import threading
import time
import unittest
from uuid import uuid4

import psycopg2

from ddp_lib.postgres_indexes import list_indexes


class CatalogRace(psycopg2.InternalError):
    pgcode = 'XX000'


class UniqueViolation(psycopg2.IntegrityError):
    pgcode = '23505'


class FakeConnection:
    def __init__(self, autocommit):
        self.autocommit = autocommit


class FakeCursor:
    """Answers the name query, then one inspect query per index name.

    ``script`` is a list of steps; each step is a list of names or an exception
    for the name query, and the rows dict maps index names to catalog rows
    (None when the index has been dropped in the meantime).
    """

    def __init__(self, script, rows, autocommit=True):
        self.script = list(script)
        self.rows = rows
        self.connection = FakeConnection(autocommit)
        self.result = None
        self.name_queries = 0

    def execute(self, sql, params):
        if 'SELECT i.relname FROM pg_index' in sql:
            self.name_queries += 1
            step = self.script.pop(0)
            if isinstance(step, Exception):
                raise step
            self.result = [(name,) for name in step]
            return
        name = params[1]
        row = self.rows.get(name)
        if isinstance(row, list):  # one answer per attempt
            row = row.pop(0)
        if isinstance(row, Exception):
            raise row
        self.result = row

    def fetchall(self):
        return self.result

    def fetchone(self):
        return self.result


def row(name):
    return (1, 's', 't', name, f'CREATE INDEX {name} ON s.t (c)', True, True, False, False, 'r', None)


class ListIndexesRaceTests(unittest.TestCase):
    def test_index_dropped_between_reads_is_left_out(self):
        cursor = FakeCursor([['a', 'b']], {'a': row('a'), 'b': None})
        self.assertEqual([p.index_name for p in list_indexes(cursor, 's', 't')], ['a'])

    def test_catalog_race_retries_on_autocommit(self):
        cursor = FakeCursor([CatalogRace('could not open relation'), ['a']], {'a': row('a')})
        self.assertEqual([p.index_name for p in list_indexes(cursor, 's', 't')], ['a'])
        self.assertEqual(cursor.name_queries, 2)

    def test_catalog_race_during_inspect_retries_that_index(self):
        rows = {'a': [CatalogRace('cache lookup failed for index'), None], 'b': row('b')}
        cursor = FakeCursor([['a', 'b']], rows)
        self.assertEqual([p.index_name for p in list_indexes(cursor, 's', 't')], ['b'])
        self.assertEqual(cursor.name_queries, 1)

    def test_catalog_race_inside_transaction_is_raised(self):
        cursor = FakeCursor([CatalogRace('could not open relation'), ['a']], {'a': row('a')},
                            autocommit=False)
        with self.assertRaises(CatalogRace):
            list_indexes(cursor, 's', 't')
        self.assertEqual(cursor.name_queries, 1)

    def test_other_errors_are_not_retried(self):
        cursor = FakeCursor([UniqueViolation('boom'), ['a']], {'a': row('a')})
        with self.assertRaises(UniqueViolation):
            list_indexes(cursor, 's', 't')
        self.assertEqual(cursor.name_queries, 1)

    def test_retries_are_bounded(self):
        cursor = FakeCursor([CatalogRace('x')] * 3 + [['a']], {'a': row('a')})
        with self.assertRaises(CatalogRace):
            list_indexes(cursor, 's', 't', attempts=3)
        self.assertEqual(cursor.name_queries, 3)


@unittest.skipUnless(os.getenv('QUEUE_TEST_POSTGRES_DSN'), 'needs disposable PostgreSQL')
class ListIndexesConcurrentDropTests(unittest.TestCase):
    """Listing keeps working while another session drops and recreates indexes."""

    def setUp(self):
        self.dsn = os.environ['QUEUE_TEST_POSTGRES_DSN']
        self.connection = psycopg2.connect(self.dsn)
        self.connection.autocommit = True
        self.cursor = self.connection.cursor()
        self.schema = 'race_' + uuid4().hex[:12]
        self.cursor.execute(f'CREATE SCHEMA "{self.schema}"')
        self.cursor.execute(f'CREATE TABLE "{self.schema}".t (c int)')
        for i in range(40):
            self.cursor.execute(f'CREATE INDEX "i{i:02}" ON "{self.schema}".t (c)')

    def tearDown(self):
        self.cursor.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.connection.close()

    def test_listing_survives_concurrent_drop_and_create(self):
        stop = threading.Event()
        errors = []

        def churn():
            other = psycopg2.connect(self.dsn)
            other.autocommit = True
            with other.cursor() as cursor:
                while not stop.is_set():
                    for i in range(0, 40, 2):
                        cursor.execute(f'DROP INDEX IF EXISTS "{self.schema}"."i{i:02}"')
                        cursor.execute(f'CREATE INDEX IF NOT EXISTS "i{i:02}" ON "{self.schema}".t (c)')
            other.close()

        worker = threading.Thread(target=churn)
        worker.start()
        listings = 0
        try:
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline:
                try:
                    found = list_indexes(self.cursor, self.schema, 't')
                except Exception as exc:  # the regression being guarded
                    errors.append(exc)
                    continue
                listings += 1
                self.assertTrue(all(p is not None for p in found))
                self.assertTrue({f'i{i:02}' for i in range(1, 40, 2)} <= {p.index_name for p in found})
        finally:
            stop.set()
            worker.join()
        self.assertGreater(listings, 0)
        self.assertEqual(errors, [])


if __name__ == '__main__':
    unittest.main()

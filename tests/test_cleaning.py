"""Lossless serialization and real PostgreSQL publication regressions.

Run with CLEANING_TEST_POSTGRES_DSN. Each database test owns a unique schema.
"""
import os
import unittest
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal
from uuid import uuid4

try:
    import pandas as pd
    import psycopg2
    from pandas.testing import assert_frame_equal
except ImportError:
    # These tests need pandas + psycopg2 (ddp-lib[cleaning]).
    raise unittest.SkipTest("Install ddp-lib[cleaning] to run cleaning tests")

from ddp_lib.cleaning import (
    COPY_BUFFER_SIZE, CopyStream, copy_dataframe, map_df_to_postgres_types,
    publish_staged_dataframe, qualified_table, read_dataframe, stage_dataframe,
)


def frame(values):
    # Build a DataFrame of object columns so pandas does not guess int/float.
    return pd.DataFrame({name: pd.Series(items, dtype=object) for name, items in values.items()})


class SerializationTests(unittest.TestCase):
    """No database. Checks types and COPY text in memory."""

    def test_type_inference_checks_all_rows_and_does_not_guess_numeric_or_json_text(self):
        # "001" and '{"a": 1}' must stay TEXT, not number/json.
        # Huge int + Decimal → NUMERIC (not float).
        data = frame({"text": ["001", " 1.20 ", "[NULL]", '{"a": 1}'],
                      "numeric": [2**64, Decimal("1.000000000000000001"), None, 1]})
        self.assertEqual(map_df_to_postgres_types(data), {"text": "TEXT", "numeric": "NUMERIC"})
        # Mixed types in one column (string then bool) must fail, even if the
        # mismatch is after the first 1000 rows (no "sample only" inference).
        with self.assertRaisesRegex(ValueError, "incompatible"):
            map_df_to_postgres_types(frame({"mixed": ["ok"] * 1001 + [False]}))

    def test_duplicate_empty_and_truncated_identifiers_are_rejected(self):
        # Postgres cannot use: no columns, duplicate names, too-long names, non-string names.
        for data in (pd.DataFrame(), pd.DataFrame([[1, 2]], columns=["x", "x"]),
                     pd.DataFrame({"é" * 32: [1]}), pd.DataFrame({1: [1]})):
            with self.subTest(columns=data.columns):
                with self.assertRaises(ValueError):
                    map_df_to_postgres_types(data)

    def test_stream_is_bounded_even_for_wide_rows_and_huge_fields(self):
        # Huge tab string + literal \N text + real NULL.
        # Each read() chunk must stay ≤ 64KiB. DataFrame must not be mutated.
        data = frame({"one": ["\t" * (3 * COPY_BUFFER_SIZE), ""], "two": [r"\N", None]})
        original = data.copy(deep=True)
        stream = CopyStream(data)
        chunks = []
        while True:
            chunk = stream.read(10 * COPY_BUFFER_SIZE)
            if not chunk:
                break
            self.assertLessEqual(len(chunk), COPY_BUFFER_SIZE)
            chunks.append(chunk)
        # Tabs escaped as \t; text \N escaped as \\N; SQL NULL is \N.
        self.assertEqual("".join(chunks), r"\t" * (3 * COPY_BUFFER_SIZE) + "\t" + r"\\N" + "\n\t\\N\n")
        assert_frame_equal(data, original)

    def test_invalid_values_have_row_and_column_diagnostics(self):
        # Bad values: NUL byte, inf in JSON, too-precise timestamp.
        # Error must say row 2, column 'value' (row 1 is None and is fine).
        for value in ("a\x00b", {"n": float("inf")}, pd.Timestamp("2025-01-01 00:00:00.000000001")):
            stream = CopyStream(frame({"value": [None, value]}))
            with self.assertRaisesRegex(ValueError, "row 2, column 'value'"):
                while stream.read():
                    pass


@unittest.skipUnless(os.getenv("CLEANING_TEST_POSTGRES_DSN"), "Set CLEANING_TEST_POSTGRES_DSN")
class DatabaseTests(unittest.TestCase):
    """Needs a real Postgres. Each test gets its own schema."""

    def setUp(self):
        self.conn = psycopg2.connect(os.environ["CLEANING_TEST_POSTGRES_DSN"])
        self.schema = "cleaning_test_" + uuid4().hex
        self.table = 'quoted "output'  # odd name: quoting must work
        self.final = qualified_table(self.schema, self.table)
        self.cur = self.conn.cursor()
        self.cur.execute(f'CREATE SCHEMA "{self.schema}"')
        self.conn.commit()

    def tearDown(self):
        self.conn.rollback()
        self.cur.execute(f'DROP SCHEMA "{self.schema}" CASCADE')
        self.conn.commit()
        self.cur.close()
        self.conn.close()

    def publish(self, data):
        # Stage into TEMP, swap into live table, then COMMIT (caller owns the transaction).
        stage = stage_dataframe(self.cur, data)
        publish_staged_dataframe(self.cur, self.schema, self.table, stage)
        self.conn.commit()

    def old_output(self):
        # Last good table + a view and an index (dependents must survive later tests).
        self.publish(frame({"id": [1], "value": ["old"]}))
        self.cur.execute(f'CREATE VIEW "{self.schema}".dependent AS SELECT * FROM {self.final}')
        self.cur.execute(f'CREATE INDEX value_idx ON {self.final} (value)')
        self.conn.commit()

    def rows(self):
        self.cur.execute(f"SELECT * FROM {self.final} ORDER BY 1")
        return self.cur.fetchall()

    def test_round_trip_every_supported_value_without_mutation(self):
        # Write many tricky values, read them back, they must match exactly.
        # Then publish the loaded frame again (second hop) so decimals still match.
        values = [None, "", "[NULL]", r"\N", "NULL", "NaN", "None", "001", "  whitespace \t\r\n ",
                  'tab\tnewline\nreturn\r"quoted"\\backslash', "é中文🙂", "[]", '{"a": 1}']
        n = len(values)
        data = frame({
            'text "column': values,
            "integer": [2**63 - 1, None] + [-(2**63)] * (n - 2),
            "huge": [2**80, None] + [-(2**80)] * (n - 2),
            "decimal": [Decimal("123456789012345678901234567890.123456789012345678901"), None] + [Decimal("0.000000000000000001")] * (n - 2),
            "json": [{"nested": [None, "\t\n ", Decimal("1.000000000000000001"), 2**80]}, None] + [[]] * (n - 2),
            "bytes": [b"\x00\xff\t\r\n\\", None] + [memoryview(b"binary")] * (n - 2),
            "day": [date(1600, 1, 1), None] + [date(2024, 2, 29)] * (n - 2),
            "stamp": [datetime(2024, 1, 1, 12, 30, 15, 123456), None] + [datetime(2020, 1, 1)] * (n - 2),
            "aware": [datetime(2024, 1, 1, tzinfo=timezone(timedelta(hours=3))), None] + [datetime(2020, 1, 1, tzinfo=timezone.utc)] * (n - 2),
            "flag": [True, None] + [False] * (n - 2),
        })
        before = data.copy(deep=True)
        self.publish(data)
        loaded = read_dataframe(self.conn, self.schema, self.table)
        for column in data:
            actual = list(loaded[column])
            expected = list(data[column])
            if column == "bytes":
                actual = [bytes(v) if v is not None else None for v in actual]
                expected = [bytes(v) if v is not None else None for v in expected]
            self.assertEqual(actual, expected, column)
        assert_frame_equal(data, before)  # publish must not change the input frame
        self.publish(loaded)
        self.assertEqual(list(read_dataframe(self.conn, self.schema, self.table)["decimal"]), list(data["decimal"]))

    def test_json_scalars_and_json_null_remain_distinct_from_sql_null(self):
        # JSON null (the value null) is not SQL NULL (empty cell).
        from ddp_lib.cleaning import JSON_NULL
        self.cur.execute(f"CREATE TABLE {self.final} (id bigint, payload jsonb)")
        inputs = ['null', '"[NULL]"', '"  text \t "', 'true', '1.000000000000000000001', '[]', '{}', None]
        inputs = [value.replace("\t", r"\t") if value else value for value in inputs]
        for idx, value in enumerate(inputs):
            self.cur.execute(f"INSERT INTO {self.final} VALUES (%s, %s::jsonb)", (idx, value))
        self.conn.commit()
        loaded = read_dataframe(self.conn, self.schema, self.table)
        self.assertIs(loaded['payload'][0], JSON_NULL)  # JSON null
        self.assertIsNone(loaded['payload'][7])         # SQL NULL
        self.publish(loaded)
        self.cur.execute(f"SELECT payload::text, payload IS NULL FROM {self.final} ORDER BY id")
        actual = self.cur.fetchall()
        self.assertEqual(actual[0], ('null', False))
        self.assertEqual(actual[1], ('"[NULL]"', False))
        self.assertEqual(actual[4], ('1.000000000000000000001', False))
        self.assertEqual(actual[7], (None, True))

    def test_nullable_pandas_types_and_unsigned_64_bit(self):
        # Pandas Int64/UInt64/boolean/string with NA must round-trip.
        # UInt64 max does not fit in BIGINT → stored as NUMERIC (Decimal).
        data = pd.DataFrame({"i": pd.Series([2**63-1, None], dtype="Int64"),
                             "u": pd.Series([2**64-1, None], dtype="UInt64"),
                             "b": pd.Series([True, None], dtype="boolean"),
                             "s": pd.Series(["[NULL]", None], dtype="string")})
        self.publish(data)
        self.assertEqual(self.rows(), [(2**63-1, Decimal(2**64-1), True, "[NULL]"), (None, None, None, None)])

    def test_copy_failure_rolls_back_staging_and_preserves_dependencies(self):
        # Bad COPY must not delete the old table or its view.
        self.old_output()
        with self.assertRaises(psycopg2.Error):
            self.cur.execute('CREATE TEMP TABLE bad_copy (id bigint) ON COMMIT DROP')
            copy_dataframe(self.cur, frame({"id": [1, "invalid integer"]}), "pg_temp", "bad_copy")
        self.conn.rollback()
        self.assertEqual(self.rows(), [(1, "old")])
        self.cur.execute(f'SELECT * FROM "{self.schema}".dependent')
        self.assertEqual(self.cur.fetchall(), [(1, "old")])
        # A later good publish still works.
        self.publish(frame({"id": [1], "value": ["retry"]}))
        self.assertEqual(self.rows(), [(1, "retry")])

    def test_success_preserves_oid_views_indexes_and_reader_snapshot(self):
        # Same table object (OID), view, and index after a successful replace.
        # Another connection that started before COMMIT still sees old rows (MVCC).
        self.old_output()
        self.cur.execute("SELECT %s::regclass::oid", (self.final,))
        oid = self.cur.fetchone()[0]
        reader = psycopg2.connect(os.environ["CLEANING_TEST_POSTGRES_DSN"])
        reader.set_session(isolation_level="REPEATABLE READ")
        try:
            with reader.cursor() as cursor:
                cursor.execute("SELECT count(*) FROM pg_class")  # pin the snapshot
                self.publish(frame({"id": [2], "value": ["new"]}))
                cursor.execute(f"SELECT * FROM {self.final}")
                self.assertEqual(cursor.fetchall(), [(1, "old")])
            self.cur.execute(f'SELECT * FROM "{self.schema}".dependent')
            self.assertEqual(self.cur.fetchall(), [(2, "new")])
            self.cur.execute("SELECT %s::regclass::oid", (self.final,))
            self.assertEqual(self.cur.fetchone()[0], oid)
            self.cur.execute("SELECT count(*) FROM pg_indexes WHERE schemaname=%s", (self.schema,))
            self.assertEqual(self.cur.fetchone()[0], 1)
        finally:
            reader.close()

    def test_readers_see_old_output_until_publication_commits(self):
        # After DELETE+INSERT but BEFORE commit, other sessions still see "old".
        self.old_output()
        observer = psycopg2.connect(os.environ["CLEANING_TEST_POSTGRES_DSN"])
        observer.autocommit = True
        try:
            stage = stage_dataframe(self.cur, frame({"id": [2], "value": ["new"]}))
            publish_staged_dataframe(self.cur, self.schema, self.table, stage)
            with observer.cursor() as reader:
                reader.execute("SET statement_timeout = '2s'")
                reader.execute(f'SELECT * FROM "{self.schema}".dependent')
                self.assertEqual(reader.fetchall(), [(1, "old")])
                self.conn.commit()
                reader.execute(f'SELECT * FROM "{self.schema}".dependent')
                self.assertEqual(reader.fetchall(), [(2, "new")])
        finally:
            observer.close()

    def test_schema_change_with_dependencies_fails_without_cascade(self):
        # New columns would DROP the table. A view blocks DROP RESTRICT.
        # Old table and view must stay. No CASCADE.
        self.old_output()
        with self.assertRaises(psycopg2.errors.DependentObjectsStillExist):
            self.publish(frame({"different": ["new"]}))
        self.conn.rollback()
        self.assertEqual(self.rows(), [(1, "old")])
        self.cur.execute(f'SELECT * FROM "{self.schema}".dependent')
        self.assertEqual(self.cur.fetchall(), [(1, "old")])

    def test_schema_change_without_dependencies_and_empty_replacement(self):
        # No view: column rename is allowed. Empty frame with columns is a valid replace.
        self.publish(frame({"old_column": ["old"]}))
        self.publish(frame({"new_column": [2**80]}))
        self.assertEqual(self.rows(), [(Decimal(2**80),)])
        empty = read_dataframe(self.conn, self.schema, self.table).iloc[:0]
        self.publish(empty)
        self.assertEqual(self.rows(), [])
        self.assertEqual(map_df_to_postgres_types(read_dataframe(self.conn, self.schema, self.table)), {"new_column": "NUMERIC"})

    def test_final_constraint_failure_and_cascading_foreign_keys_preserve_old_output(self):
        # CHECK fail → rollback, old rows stay.
        # ON DELETE CASCADE on a child → refuse publish, child row stays.
        self.old_output()
        self.cur.execute(f"ALTER TABLE {self.final} ADD PRIMARY KEY (id)")
        self.cur.execute(f"ALTER TABLE {self.final} ADD CHECK (value <> 'bad')")
        self.conn.commit()
        with self.assertRaises(psycopg2.errors.CheckViolation):
            self.publish(frame({"id": [1], "value": ["bad"]}))
        self.conn.rollback()
        self.assertEqual(self.rows(), [(1, "old")])
        self.cur.execute(f'CREATE TABLE "{self.schema}".child (id bigint REFERENCES {self.final} ON DELETE CASCADE)')
        self.cur.execute(f'INSERT INTO "{self.schema}".child VALUES (1)')
        self.conn.commit()
        with self.assertRaisesRegex(RuntimeError, "cascading foreign keys"):
            self.publish(frame({"id": [1], "value": ["new"]}))
        self.conn.rollback()
        self.assertEqual(self.rows(), [(1, "old")])
        self.cur.execute(f'SELECT * FROM "{self.schema}".child')
        self.assertEqual(self.cur.fetchall(), [(1,)])


if __name__ == "__main__":
    unittest.main()

"""Population index identity contracts, runnable without PostgreSQL or Core."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from ddp_lib import population_indexes as indexes


def index_doc(schema="mart", table="output", name="id_idx", column="id", concurrently=False):
    statement = f'CREATE INDEX {"CONCURRENTLY " if concurrently else ""}"{name}" ON "{schema}"."{table}" ({column})'
    return dict(schema=schema, table=table, index_name=name, sql=statement,
                status="created", last_run_at="previous run")


class IdentityTests(unittest.TestCase):
    def test_quoted_case_and_mixed_quoting_follow_postgres_identity(self):
        meta = indexes.parse_create_index('CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS IDX ON "Mart".Output (id)')
        self.assertEqual(meta, dict(index_name="idx", schema="Mart", table="output"))
        meta = indexes.parse_create_index('CREATE INDEX "My""Index" ON mart."Output" (id)')
        self.assertEqual(meta, dict(index_name='My"Index', schema="mart", table="Output"))

    def test_legacy_unquoted_uppercase_metadata_is_folded(self):
        doc = dict(index_name="IDX", schema="MART", table="OUTPUT", sql="CREATE INDEX IDX ON MART.OUTPUT (id)")
        self.assertEqual(indexes.resolve_query_index(doc)[0],
                         dict(index_name="idx", schema="mart", table="output"))

    def test_mismatched_or_missing_identity_never_executes_sql(self):
        for field in ("schema", "table", "index_name"):
            for value in (None, "somewhere_else"):
                doc = index_doc()
                doc[field] = value
                cursor = Mock()
                with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                    indexes.ensure_query_index(cursor, doc)
                cursor.execute.assert_not_called()

    def test_unqualified_sql_is_rejected(self):
        with self.assertRaises(ValueError):
            indexes.parse_create_index("CREATE INDEX idx ON output (id)")

    def test_display_name_does_not_override_physical_identity(self):
        doc = index_doc(table="display_name")
        population = SimpleNamespace(schema="mart", table_name="output", name="display_name",
                                     query_indexes=[doc], update=Mock())
        indexes.prepare_population_query_indexes(population)
        self.assertEqual(doc["status"], "created")  # An upstream physical table, not the target.


if __name__ == "__main__":
    unittest.main()

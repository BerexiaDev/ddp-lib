"""Population index lifecycle shared by Core API and Sync.

Index identities are physical PostgreSQL schema/table names, never population
display names or search_path lookups. Callers supply the population document and
connector factory. PostgreSQL operations require psycopg2; importing this module
and resolving index identities do not.
"""
import re
from datetime import datetime, timezone


class PopulationIndexError(RuntimeError):
    """Index/statistics failure; retry without falling back to a view rebuild."""


_IDENT = r'(?:"(?:[^"]|"")+"|[A-Za-z_][A-Za-z0-9_$]*)'
_CREATE_INDEX = re.compile(
    rf'^\s*CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:CONCURRENTLY\s+)?'
    rf'(?:IF\s+NOT\s+EXISTS\s+)?(?P<index_name>{_IDENT})\s+ON\s+'
    rf'(?:ONLY\s+)?(?P<schema>{_IDENT})\s*\.\s*(?P<table>{_IDENT})(?=\s|\()',
    re.IGNORECASE,
)


def _identifier(value):
    return value[1:-1].replace('""', '"') if value.startswith('"') else value.lower()


def parse_create_index(statement):
    match = _CREATE_INDEX.match(statement or "")
    if not match:
        raise ValueError("Expected CREATE INDEX with an explicit schema.table target.")
    return {key: _identifier(value) for key, value in match.groupdict().items()}


def resolve_query_index(index_doc):
    statement = index_doc.get("normalized_sql") or index_doc.get("sql")
    meta = parse_create_index(statement)
    match = _CREATE_INDEX.match(statement)
    for key, value in meta.items():
        # Earlier API versions persisted unquoted identifiers without PostgreSQL's
        # lower-case folding. Accept that spelling, but never remap display names.
        legacy = match.group(key)
        if index_doc.get(key) not in (value, legacy):
            raise ValueError(f"Query index {key} metadata does not match its SQL: {index_doc.get(key)!r}.")
    return meta, statement


def _index_state(cursor, meta):
    cursor.execute(
        """
        SELECT table_namespace.nspname, table_class.relname,
               index_meta.indisvalid, index_meta.indisready
        FROM pg_class index_class
        JOIN pg_namespace namespace ON namespace.oid = index_class.relnamespace
        LEFT JOIN pg_index index_meta ON index_meta.indexrelid = index_class.oid
        LEFT JOIN pg_class table_class ON table_class.oid = index_meta.indrelid
        LEFT JOIN pg_namespace table_namespace ON table_namespace.oid = table_class.relnamespace
        WHERE namespace.nspname = %s AND index_class.relname = %s
        """,
        (meta["schema"], meta["index_name"]),
    )
    row = cursor.fetchone()
    if row and row[:2] != (meta["schema"], meta["table"]):
        raise ValueError(f"Index name {meta['schema']}.{meta['index_name']} is used by another relation.")
    return row


def ensure_query_index(cursor, index_doc):
    """Create a missing index or repair an invalid one on the exact relation."""
    meta, statement = resolve_query_index(index_doc)
    state = _index_state(cursor, meta)
    if state and state[2] and state[3]:
        return meta
    if state:
        from psycopg2.sql import SQL, Identifier

        cursor.execute(SQL("DROP INDEX {}.{}").format(
            Identifier(meta["schema"]), Identifier(meta["index_name"])))
    try:
        cursor.execute(statement)
    except Exception as exc:
        # Another application may have created it between lookup and CREATE.
        # Only accept that race after verifying the resulting index below.
        if getattr(exc, "pgcode", None) not in ("42P07", "42710"):
            raise
    state = _index_state(cursor, meta)
    if not state or not state[2] or not state[3]:
        raise RuntimeError(f"Index {meta['schema']}.{meta['index_name']} is not valid and ready.")
    return meta


def _configured_indexes(population):
    docs = list(getattr(population, "query_indexes", None) or [])
    # Preserve the older column-index configuration as well as query indexes.
    def quote(value):
        return '"' + value.replace('"', '""') + '"'

    for column in getattr(population, "indexes", None) or []:
        name = f"idx_{population.schema}_{population.table_name}_{column}"
        # PostgreSQL truncates identifiers to NAMEDATALEN - 1 bytes.
        name = name.encode("utf-8")[:63].decode("utf-8", errors="ignore")
        docs.append({
            "schema": population.schema, "table": population.table_name, "index_name": name,
            "sql": f"CREATE INDEX {quote(name)} ON {quote(population.schema)}.{quote(population.table_name)} ({quote(column)})",
        })
    return docs


def _is_target(population, meta):
    return (meta["schema"], meta["table"]) == (population.schema, population.table_name)


def prepare_population_query_indexes(population):
    """Validate all identities and invalidate the previous target restore status."""
    docs = getattr(population, "query_indexes", None) or []
    try:
        for doc in docs:
            meta, _ = resolve_query_index(doc)
            if _is_target(population, meta):
                doc["status"] = "pending"
                doc.pop("last_error", None)
    except Exception as exc:
        doc.update(status="error", last_error=str(exc))
        raise PopulationIndexError(str(exc)) from exc
    finally:
        if docs:
            population.query_indexes = docs
            population.update()


def apply_population_query_indexes(population, connector_factory, *,
                                   include_population_target=True, only_population_target=False):
    """Restore selected indexes, then ANALYZE, then persist successful status.

    Call with include_population_target=False before computation, and with
    only_population_target=True only after the view's publication commit.
    The target phase analyzes even populations without configured indexes.
    """
    connection = cursor = None
    selected = []
    try:
        for doc in _configured_indexes(population):
            meta, _ = resolve_query_index(doc)
            target = _is_target(population, meta)
            if only_population_target and not target:
                continue
            if not include_population_target and target:
                continue
            selected.append(doc)
        if not selected and not only_population_target:
            return

        from psycopg2.sql import SQL, Identifier

        connection = connector_factory(population.schema).get_connection()
        if not connection:
            raise ConnectionError("Unable to connect to the datamart database.")
        # CREATE INDEX CONCURRENTLY must run outside a transaction.
        connection.autocommit = True
        cursor = connection.cursor()
        analyze_targets = {(population.schema, population.table_name)} if only_population_target else set()
        for doc in selected:
            meta = ensure_query_index(cursor, doc)
            analyze_targets.add((meta["schema"], meta["table"]))
        for schema, table in sorted(analyze_targets):
            cursor.execute(SQL("ANALYZE {}.{}").format(Identifier(schema), Identifier(table)))

        now = datetime.now(timezone.utc).isoformat()
        for doc in selected:
            doc.update(status="created", last_run_at=now)
            doc.pop("last_error", None)
    except Exception as exc:
        for doc in selected:
            doc.update(status="error", last_run_at=datetime.now(timezone.utc).isoformat(), last_error=str(exc))
        raise PopulationIndexError(str(exc)) from exc
    finally:
        try:
            if cursor:
                cursor.close()
        finally:
            if connection:
                connection.close()
        if selected:
            population.update()

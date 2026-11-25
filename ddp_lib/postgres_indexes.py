"""PostgreSQL catalog and session primitives for dataset index lifecycle.

No application metadata or authorization policy lives in this module.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from hashlib import sha256

from psycopg2.sql import SQL, Identifier


class IndexLockBusy(RuntimeError):
    """The lock is already taken. The caller can retry; this does not wait."""


@dataclass(frozen=True)
class PhysicalIndex:
    """One index as PostgreSQL reports it. This is not the saved DDP request."""

    oid: int
    schema: str
    table: str
    index_name: str
    definition: str  # CREATE INDEX text returned by PostgreSQL.
    valid: bool  # False when a failed build is still in the catalog.
    ready: bool  # False while PostgreSQL is still building the index.
    primary: bool
    constraint_owned: bool  # True for a PRIMARY KEY or UNIQUE constraint.
    relation_kind: str  # Kind of the table. "p" means a partitioned table.
    comment: str | None


def inspect_index(cursor, schema, name):
    """Read one index from the catalog. Return None when that name is missing."""
    # Column order matches PhysicalIndex. t.relkind is the table kind, not the index kind.
    cursor.execute('''SELECT i.oid, n.nspname, t.relname, i.relname,
        pg_get_indexdef(x.indexrelid), x.indisvalid, x.indisready, x.indisprimary,
        EXISTS (SELECT 1 FROM pg_constraint c WHERE c.conindid=i.oid),
        t.relkind, obj_description(i.oid, 'pg_class')
        FROM pg_class i JOIN pg_namespace n ON n.oid=i.relnamespace
        LEFT JOIN pg_index x ON x.indexrelid=i.oid
        LEFT JOIN pg_class t ON t.oid=x.indrelid
        WHERE n.nspname=%s AND i.relname=%s''', (schema, name))
    row = cursor.fetchone()
    return PhysicalIndex(*row) if row else None


def list_indexes(cursor, schema, table):
    """Return every index on this table, in name order."""
    cursor.execute('''SELECT i.relname FROM pg_index x
        JOIN pg_class i ON i.oid=x.indexrelid JOIN pg_class t ON t.oid=x.indrelid
        JOIN pg_namespace n ON n.oid=t.relnamespace
        WHERE n.nspname=%s AND t.relname=%s ORDER BY i.relname''', (schema, table))
    return [inspect_index(cursor, schema, name) for name, in cursor.fetchall()]


def index_ready(cursor, physical):
    """Verify an index and every attached index covering its partition tree."""
    # A ready parent alone does not prove that all its partitions are ready.
    if not physical or not physical.valid or not physical.ready:
        return False
    if physical.relation_kind != 'p':
        return True
    # pg_get_indexdef renders ON ONLY even for a complete partitioned index.
    # Compare definitions separately and require coverage/readiness at every
    # level, including intermediate partitioned relations and empty branches.
    cursor.execute('''WITH RECURSIVE
        tables(oid) AS (
            SELECT indrelid FROM pg_index WHERE indexrelid=%s
            UNION ALL
            SELECT h.inhrelid FROM pg_inherits h JOIN tables t ON h.inhparent=t.oid
        ), indexes(oid) AS (
            SELECT %s::oid
            UNION ALL
            SELECT h.inhrelid FROM pg_inherits h JOIN indexes i ON h.inhparent=i.oid
        )
        SELECT EXISTS (SELECT 1 FROM tables) AND NOT EXISTS (
            SELECT 1 FROM tables t LEFT JOIN (
                SELECT x.* FROM pg_index x JOIN indexes i ON i.oid=x.indexrelid
            ) x ON x.indrelid=t.oid
            WHERE x.indexrelid IS NULL OR NOT x.indisvalid OR NOT x.indisready OR NOT x.indislive
        )''', (physical.oid, physical.oid))
    return bool(cursor.fetchone()[0])


def lock_id(key):
    """Turn a text key into the number PostgreSQL uses for an advisory lock."""
    # The same key always hashes to the same signed 64-bit number.
    return int.from_bytes(sha256(key.encode('utf8')).digest()[:8], 'big', signed=True)


@contextmanager
def advisory_lock(connection, key):
    """Hold a session lock, or fail at once when another session already holds it."""
    with connection.cursor() as cur:
        # This does not wait. False means a dataset or index change is already running.
        cur.execute('SELECT pg_try_advisory_lock(%s)', (lock_id(key),))
        if not cur.fetchone()[0]:
            raise IndexLockBusy('Dataset/index is busy; retry reconciliation')
    try:
        yield connection
    finally:
        # A closed connection has already released its locks.
        if not connection.closed:
            with connection.cursor() as cur:
                cur.execute('SELECT pg_advisory_unlock(%s)', (lock_id(key),))


# Connections already opened by this task, so nested index work can reuse them.
_sessions = ContextVar('ddp_index_sessions', default=None)


@contextmanager
def dataset_session(factory, collection, dataset_id, schema, table):
    """Open one autocommit connection and lock this dataset for index changes.

    A nested call for the same dataset reuses that connection. CREATE and DROP
    INDEX CONCURRENTLY cannot run inside a transaction, so autocommit stays on.
    """
    key = f'ddp-index:v1:dataset:{collection}:{dataset_id}'
    held = _sessions.get() or {}
    if key in held:
        # This task already opened the session. Do not lock or connect again.
        yield held[key]
        return
    connection = factory()
    if connection is None:
        raise ConnectionError('Unable to connect to the datamart')
    try:
        connection.autocommit = True
        with advisory_lock(connection, key):
            # Also lock schema.table, so two records for one table cannot both change indexes.
            with advisory_lock(connection, f'ddp-index:v1:relation:{schema}:{table}'):
                token = _sessions.set(dict(held, **{key: connection}))
                try:
                    yield connection
                finally:
                    _sessions.reset(token)
    finally:
        connection.close()


def stamp(cursor, physical, receipt):
    """Mark the index as one DDP owns, and keep every other comment line."""
    prefix = '[ddp-index:v1:'
    # Remove an older DDP line, then add one new line. Other comment text stays.
    lines = (physical.comment or '').split('\n')
    original = '\n'.join(line for line in lines if not line.startswith(prefix))
    comment = original + ('\n' if original else '') + prefix + receipt + ']'
    cursor.execute(SQL('COMMENT ON INDEX {}.{} IS %s').format(
        Identifier(physical.schema), Identifier(physical.index_name)), (comment,))


def drop(cursor, physical):
    """Drop an index. Leave primary keys and constraint indexes alone."""
    if physical.primary or physical.constraint_owned or physical.table is None:
        raise ValueError('Cannot drop a constraint or non-index relation')
    # A partitioned table rejects DROP INDEX CONCURRENTLY, so it uses a normal drop.
    cursor.execute(SQL('DROP INDEX {}{}.{}').format(
        SQL('' if physical.relation_kind == 'p' else 'CONCURRENTLY '),
        Identifier(physical.schema), Identifier(physical.index_name)))

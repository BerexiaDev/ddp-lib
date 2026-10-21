"""Lossless full-cleaning I/O. PostgreSQL transactions belong to the caller.

Pandas and psycopg2 are optional runtime dependencies of the cleaning services.
No string-to-number guessing, sampled type inference, or whitespace cleanup is
performed here. Unsupported mixtures fail before publication rather than losing
values. COPY buffers at most one scalar plus a fixed-size transport buffer.
"""
import io
import json
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from numbers import Integral, Real
from uuid import uuid4


COPY_BUFFER_SIZE = 64 * 1024
PG_TYPES = {
    16: "BOOLEAN", 17: "BYTEA", 20: "BIGINT", 21: "SMALLINT", 23: "INTEGER",
    25: "TEXT", 700: "REAL", 701: "DOUBLE PRECISION", 1043: "TEXT",
    1082: "DATE", 1083: "TIME", 1114: "TIMESTAMP", 1184: "TIMESTAMPTZ",
    1186: "INTERVAL", 1266: "TIMETZ", 1700: "NUMERIC", 114: "JSON", 3802: "JSONB",
}


class JsonNull:
    """Distinguish a JSON null value from SQL NULL in a cleaning input."""
    def __repr__(self):
        return "JSON_NULL"


JSON_NULL = JsonNull()


def quote_identifier(name):
    if not isinstance(name, str) or not name or "\x00" in name or len(name.encode("utf-8")) > 63:
        raise ValueError("PostgreSQL identifiers must be nonempty strings of at most 63 UTF-8 bytes")
    return '"' + name.replace('"', '""') + '"'


def qualified_table(schema, table):
    return f"{quote_identifier(schema)}.{quote_identifier(table)}"


def is_missing(value):
    import pandas as pd
    # Containers (including a one-element list) are never SQL NULL.
    return pd.api.types.is_scalar(value) and bool(pd.isna(value))


def _kind(value):
    import numpy as np
    if isinstance(value, (bool, np.bool_)):
        return "BOOLEAN"
    if isinstance(value, Integral):
        return "BIGINT" if -(2**63) <= value < 2**63 else "NUMERIC"
    if isinstance(value, Decimal):
        return "NUMERIC"
    if isinstance(value, Real):
        return "DOUBLE PRECISION"
    if isinstance(value, str):
        return "TEXT"
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "BYTEA"
    if isinstance(value, datetime):
        return "TIMESTAMPTZ" if value.utcoffset() is not None else "TIMESTAMP"
    if isinstance(value, date):
        return "DATE"
    if isinstance(value, time):
        return "TIMETZ" if value.utcoffset() is not None else "TIME"
    if isinstance(value, timedelta):
        return "INTERVAL"
    if isinstance(value, (dict, list, JsonNull)):
        return "JSONB"
    raise TypeError(f"Unsupported cleaning value type: {type(value).__name__}")


def map_df_to_postgres_types(df, use_varchar=False, varchar_cap=255, sample_rows=1000):
    import pandas as pd
    if not isinstance(df, pd.DataFrame):
        raise ValueError("Your script must return a pandas DataFrame")
    if not len(df.columns) or not df.columns.is_unique:
        raise ValueError("Cleaning output requires nonempty, unique column names")
    out = {}
    for name in df.columns:
        quote_identifier(name)
        series = df[name]
        kinds = set()
        max_length = 0
        for value in series:
            if is_missing(value):
                continue
            kinds.add(_kind(value))
            if isinstance(value, str):
                max_length = max(max_length, len(value))
        json_type = df.attrs.get("postgres_types", {}).get(name)
        if json_type in ("JSON", "JSONB"):
            kind = json_type
        elif kinds and kinds <= {"BIGINT", "NUMERIC"}:
            kind = "NUMERIC" if "NUMERIC" in kinds else "BIGINT"
        elif len(kinds) == 1:
            kind = kinds.pop()
        elif kinds:
            raise ValueError(f"Column {name!r} mixes incompatible types: {sorted(kinds)}")
        else:
            dtype = series.dtype
            if pd.api.types.is_bool_dtype(dtype):
                kind = "BOOLEAN"
            elif pd.api.types.is_integer_dtype(dtype):
                kind = "NUMERIC" if str(dtype).lower() == "uint64" else "BIGINT"
            elif pd.api.types.is_float_dtype(dtype):
                kind = "DOUBLE PRECISION"
            elif isinstance(dtype, pd.DatetimeTZDtype):
                kind = "TIMESTAMPTZ"
            elif pd.api.types.is_datetime64_any_dtype(dtype):
                kind = "TIMESTAMP"
            elif pd.api.types.is_timedelta64_dtype(dtype):
                kind = "INTERVAL"
            else:
                kind = df.attrs.get("postgres_types", {}).get(name, "TEXT")
                if kind not in PG_TYPES.values():
                    kind = "TEXT"
        if kind == "TEXT" and use_varchar and 0 < max_length <= varchar_cap:
            kind = f"VARCHAR({max_length})"
        out[name] = kind
    return out


def _json_text(value):
    """Encode JSON numbers without routing Decimal through a binary float."""
    if isinstance(value, JsonNull):
        return "null"
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("JSON numbers must be finite")
        return str(value)
    if isinstance(value, dict):
        if not all(isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return "{" + ",".join(json.dumps(key, ensure_ascii=False) + ":" + _json_text(item)
                               for key, item in value.items()) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_json_text(item) for item in value) + "]"
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def serialize_copy_value(value):
    import numpy as np
    if is_missing(value):
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, (bool, np.bool_)):
        return "t" if value else "f"
    if isinstance(value, (datetime, timedelta)) and getattr(value, "nanosecond", 0):
        raise ValueError("PostgreSQL timestamps cannot preserve sub-microsecond precision")
    if isinstance(value, timedelta):
        if getattr(value, "nanoseconds", 0):
            raise ValueError("PostgreSQL intervals cannot preserve sub-microsecond precision")
        return f"{value.days} days {value.seconds} seconds {value.microseconds} microseconds"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, (dict, list, JsonNull)):
        return _json_text(value)
    if isinstance(value, (str, Integral, Real, Decimal)):
        return str(value)
    raise TypeError(f"Unsupported COPY value type: {type(value).__name__}")


def _copy_fragments(df, col_types=None):
    col_types = col_types or df.attrs.get("postgres_types", {})
    for row_number, row in enumerate(df.itertuples(index=False, name=None), 1):
        for column, value in zip(df.columns, row):
            try:
                if col_types.get(column) in ("JSON", "JSONB"):
                    raw = None if is_missing(value) else _json_text(value)
                else:
                    raw = serialize_copy_value(value)
                if raw is None:
                    yield r"\N"
                else:
                    if "\x00" in raw:
                        raise ValueError("PostgreSQL text cannot contain NUL")
                    for start in range(0, len(raw), COPY_BUFFER_SIZE):
                        yield (raw[start:start + COPY_BUFFER_SIZE].replace("\\", "\\\\")
                               .replace("\t", r"\t").replace("\n", r"\n").replace("\r", r"\r"))
            except Exception as exc:
                raise ValueError(f"COPY serialization failed at row {row_number}, column {column!r}: {exc}") from exc
            yield "\n" if column == df.columns[-1] else "\t"


class CopyStream(io.TextIOBase):
    """A bounded pull stream; never materializes the full output or a full row."""
    def __init__(self, df, col_types=None):
        self.fragments = iter(_copy_fragments(df, col_types))
        self.pending = ""

    def read(self, size=COPY_BUFFER_SIZE):
        if size is None or size < 0:
            size = COPY_BUFFER_SIZE
        size = min(size, COPY_BUFFER_SIZE)
        parts = []
        remaining = size
        while remaining:
            if not self.pending:
                self.pending = next(self.fragments, "")
                if not self.pending:
                    break
            part, self.pending = self.pending[:remaining], self.pending[remaining:]
            parts.append(part)
            remaining -= len(part)
        return "".join(parts)


def copy_dataframe(cursor, df, schema, table, col_types=None):
    columns = ", ".join(quote_identifier(col) for col in df.columns)
    statement = (f"COPY {qualified_table(schema, table)} ({columns}) FROM STDIN "
                 "WITH (FORMAT text, DELIMITER E'\\t', NULL E'\\\\N')")
    with CopyStream(df, col_types) as stream:
        cursor.copy_expert(statement, stream, size=COPY_BUFFER_SIZE)


def read_dataframe(connection, schema, table, limit=None):
    """Build object columns directly: pandas inference can round nullable int64.

    Cursor-local JSON parsers retain fractional numbers as Decimal. Object dtype
    preserves native dates, bytes, booleans, NULL and numeric values unchanged.
    Full cleaning still loads all input rows; fetches use bounded batches.
    """
    import pandas as pd
    from psycopg2.extras import register_default_json, register_default_jsonb
    with connection.cursor() as cursor:
        def loads(value):
            result = json.loads(value, parse_float=Decimal)
            return JSON_NULL if result is None else result
        register_default_json(cursor, loads=loads)
        register_default_jsonb(cursor, loads=loads)
        statement = f"SELECT * FROM {qualified_table(schema, table)}"
        params = ()
        if limit is not None:
            statement += " LIMIT %s"
            params = (int(limit),)
        cursor.execute(statement, params)
        names = [col.name for col in cursor.description]
        types = {col.name: PG_TYPES.get(col.type_code, "TEXT") for col in cursor.description}
        columns = [[] for _ in names]
        while True:
            rows = cursor.fetchmany(1000)
            if not rows:
                break
            for row in rows:
                for column, value in zip(columns, row):
                    column.append(value)
        frame = pd.DataFrame({name: pd.Series(values, dtype=object)
                              for name, values in zip(names, columns)})
        frame.attrs["postgres_types"] = types
        return frame


def stage_dataframe(cursor, df):
    types = map_df_to_postgres_types(df)
    stage = "cleaning_" + uuid4().hex
    columns = ", ".join(f"{quote_identifier(name)} {kind}" for name, kind in types.items())
    cursor.execute(f"CREATE TEMP TABLE {quote_identifier(stage)} ({columns}) ON COMMIT DROP")
    copy_dataframe(cursor, df, "pg_temp", stage, types)
    return stage


def table_signature(cursor, schema, table):
    cursor.execute("""SELECT attname, atttypid, atttypmod FROM pg_attribute
        WHERE attrelid = to_regclass(%s) AND attnum > 0 AND NOT attisdropped
        ORDER BY attnum""", (qualified_table(schema, table),))
    return cursor.fetchall()


def publish_staged_dataframe(cursor, schema, table, stage):
    """Publish inside the caller's transaction, without CASCADE or commits.

    Same-schema refreshes retain the table OID, views, indexes and grants.
    Schema changes use DROP RESTRICT: incompatible dependencies fail safely and
    require an explicit migration. The fully loaded temporary stage is discarded
    by either COMMIT or ROLLBACK, so retries never inherit partial output.
    """
    final = qualified_table(schema, table)
    source = qualified_table("pg_temp", stage)
    cursor.execute("SET LOCAL lock_timeout = '10s'")
    cursor.execute(f"CREATE SCHEMA IF NOT EXISTS {quote_identifier(schema)}")
    cursor.execute("SELECT to_regclass(%s)", (final,))
    exists = cursor.fetchone()[0] is not None
    if exists:
        cursor.execute(f"LOCK TABLE {final} IN SHARE ROW EXCLUSIVE MODE")
    source_signature = table_signature(cursor, "pg_temp", stage)
    if exists and table_signature(cursor, schema, table) != source_signature:
        cursor.execute(f"DROP TABLE {final} RESTRICT")
        exists = False
    if not exists:
        cursor.execute(f"CREATE TABLE {final} (LIKE {source} INCLUDING ALL)")
    else:
        cursor.execute("""SELECT conname FROM pg_constraint WHERE contype = 'f'
            AND confrelid = %s::regclass AND confdeltype IN ('c', 'n', 'd')""", (final,))
        if cursor.fetchone():
            raise RuntimeError("Full cleaning cannot publish through cascading foreign keys")
        cursor.execute("SET CONSTRAINTS ALL DEFERRED")
        cursor.execute(f"DELETE FROM {final}")
    columns = ", ".join(quote_identifier(row[0]) for row in source_signature)
    cursor.execute(f"INSERT INTO {final} ({columns}) SELECT {columns} FROM {source}")
    # Check deferred constraints before recording the successful metadata result.
    cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")

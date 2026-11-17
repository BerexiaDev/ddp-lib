"""Durable dataset index reconciliation, shared by API and worker (KAN-20)."""

from dataclasses import asdict, dataclass
from hashlib import sha256
import json
from uuid import uuid4

from ddp_lib.index_sql import parse_index, quote
from ddp_lib.index_metadata import (
    IndexConflict,
    save_intent,
    delete_intent,
    record_result,
    expected_revision,
)
from ddp_lib.postgres_indexes import (
    advisory_lock,
    dataset_session,
    inspect_index,
    list_indexes,
    index_ready,
    stamp,
    drop,
)


@dataclass(frozen=True)
class ReconcileResult:
    ready: bool
    applied: int
    errors: tuple
    revision: int

    def to_dict(self):
        return asdict(self)


class IndexRepairError(RuntimeError):
    def __init__(self, message, result=None):
        super().__init__(message)
        self.result = result


def catalog_signature(physical, definition):
    # Compare index definitions independently of their names.
    signature = parse_index(physical.definition).signature
    # PostgreSQL prints ONLY for every partitioned parent. Full-tree readiness
    # is checked separately; catalog rendering must not change saved intent.
    if physical.relation_kind == "p":
        signature["only"] = definition.only
    return signature


def receipt(entry, physical):
    definition = parse_index(entry["sql"])
    payload = [
        entry["id"],
        definition.signature,
        catalog_signature(physical, definition),
    ]
    return sha256(json.dumps(payload, sort_keys=True).encode("utf8")).hexdigest()


def owns(entry, physical):
    return "[ddp-index:v1:" + receipt(entry, physical) + "]" in (
        physical.comment or ""
    ).split("\n")


def canonical_signature(cursor, definition, schema, table):
    """Ask PostgreSQL to normalize the request against the target column types.

    A private, empty LIKE table avoids guessing which casts/default opclasses
    the server inserts. No data is copied and no target index is modified.
    """
    temp = "ddp_index_check_" + uuid4().hex
    name = "ddp_index_" + uuid4().hex
    created = False
    try:
        cursor.execute(
            f"CREATE TEMP TABLE {quote(temp)} (LIKE {quote(schema)}.{quote(table)})"
        )
        created = True
        cursor.execute(
            definition.render(
                "pg_temp", temp, name=name, concurrently=False, only=False
            )
        )
        cursor.execute(
            """SELECT pg_get_indexdef(i.indexrelid) FROM pg_index i
            JOIN pg_class t ON t.oid=i.indrelid
            WHERE t.relnamespace=pg_my_temp_schema() AND t.relname=%s""",
            (temp,),
        )
        signature = parse_index(cursor.fetchone()[0]).signature
        signature["only"] = definition.only
        return signature
    finally:
        if created:
            cursor.execute(f"DROP TABLE pg_temp.{quote(temp)}")


def validate_existing(cursor, entry, physical, schema, table):
    if physical is None:
        return
    if (physical.schema, physical.table) != (schema, table):
        raise IndexConflict("Index name is occupied by another relation")
    if physical.primary or physical.constraint_owned:
        raise IndexConflict(
            f"Constraint-owned index '{physical.index_name}' is read-only"
        )
    definition = parse_index(entry["sql"])
    if owns(entry, physical):
        return
    if not entry.get("adopt") or entry["desired_state"] != "present":
        raise IndexConflict(
            "Unmanaged index requires explicit adoption and definition review"
        )
    expected = canonical_signature(cursor, definition, schema, table)
    if catalog_signature(physical, definition) != expected:
        raise IndexConflict("Existing index has a different definition")


def apply_entry(cursor, entry, schema, table, *, concurrently=True):
    definition = parse_index(entry["sql"])
    physical = inspect_index(cursor, schema, entry["index_name"])
    if (physical and (physical.schema, physical.table) == (schema, table)
            and (physical.primary or physical.constraint_owned)):
        # Leave the primary key or unique constraint in place. A mistaken saved
        # request must not block the other indexes on this table.
        if entry["desired_state"] == "absent":
            return {"status": "deleted", "schema": schema, "table": table}
        if str(entry.get("id", "")).startswith("source:"):
            raise IndexConflict(
                f"Constraint-owned index '{physical.index_name}' is read-only"
            )
        return {
            "status": "error",
            "last_error": f"Constraint-owned index '{physical.index_name}' is read-only",
            "schema": schema,
            "table": table,
        }
    validate_existing(cursor, entry, physical, schema, table)
    if entry["desired_state"] == "absent":
        if physical:
            drop(cursor, physical)
        if inspect_index(cursor, schema, entry["index_name"]) is not None:
            raise IndexRepairError("Index still exists after DROP")
        return {"status": "deleted", "schema": schema, "table": table}
    if physical and (not physical.valid or not physical.ready):
        if physical.relation_kind == "p":
            raise IndexRepairError(
                "Invalid partitioned index requires explicit attachment review"
            )
        drop(cursor, physical)
        physical = None
    expected = entry.get("signature")
    if not physical:
        expected = canonical_signature(cursor, definition, schema, table)
        cursor.execute(
            """SELECT c.relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=%s AND c.relname=%s""",
            (schema, table),
        )
        kind = cursor.fetchone()
        try:
            cursor.execute(
                definition.render(
                    schema,
                    table,
                    concurrently=concurrently and bool(kind) and kind[0] != "p",
                )
            )
        except Exception as exc:
            # Only a failed build owned by this CREATE can acquire a receipt.
            # Duplicate-name races and connection ambiguity require review.
            if getattr(exc, "pgcode", None) not in {None, "42P07", "42710"}:
                invalid = inspect_index(cursor, schema, entry["index_name"])
                if (
                    invalid
                    and invalid.table == table
                    and not invalid.valid
                    and not invalid.constraint_owned
                    and not invalid.primary
                    and catalog_signature(invalid, definition) == expected
                ):
                    stamp(cursor, invalid, receipt(entry, invalid))
            raise
        physical = inspect_index(cursor, schema, entry["index_name"])
        if not physical or catalog_signature(physical, definition) != expected:
            raise IndexRepairError(
                "Created index does not match the requested definition"
            )
        stamp(cursor, physical, receipt(entry, physical))
    elif entry.get("adopt"):
        stamp(cursor, physical, receipt(entry, physical))
    # Report success only when the index and all its partitions are ready.
    if not index_ready(cursor, physical):
        raise IndexRepairError("Index or partition tree is not valid and ready; attachment review required")
    signature = catalog_signature(physical, definition)
    if expected is not None and signature != expected:
        raise IndexConflict(
            "Physical definition differs from the verified saved definition"
        )
    return {
        "status": "created",
        "signature": signature,
        "schema": schema,
        "table": table,
    }


def _version(entries):
    return {(e["id"], e["revision"]) for e in entries}


def reconcile(
    collection,
    dataset_id,
    schema,
    table,
    connection_factory,
    connection=None,
    *,
    concurrently=True,
    source_entries=(),
    finish=True,
):
    if connection is None:
        try:
            with dataset_session(
                connection_factory, collection.full_name, dataset_id, schema, table
            ) as conn:
                return reconcile(
                    collection,
                    dataset_id,
                    schema,
                    table,
                    connection_factory,
                    conn,
                    concurrently=concurrently,
                    source_entries=source_entries,
                    finish=finish,
                )
        except Exception as exc:
            collection.update_one(
                {"_id": dataset_id},
                {
                    "$set": {
                        "index_repair_pending": True,
                        "index_repair_error": str(exc),
                        "indexes_applied": False,
                    }
                },
            )
            if isinstance(exc, IndexRepairError):
                raise
            raise IndexRepairError(str(exc)) from exc
    if not connection.autocommit:
        raise ValueError('Index DDL requires a dedicated autocommit connection')
    # A supplied connection follows the same lock contract. PostgreSQL session
    # locks are reentrant when the caller already holds the data-write guard.
    with advisory_lock(connection, f'ddp-index:v1:dataset:{collection.full_name}:{dataset_id}'):
        with advisory_lock(connection, f'ddp-index:v1:relation:{schema}:{table}'):
            return _reconcile_snapshot(collection, dataset_id, schema, table, connection,
                                       concurrently, source_entries, finish)


def _error_text(errors):
    return "; ".join(
        f"{item['index_name']}: {item['error']}" if item.get("index_name") else item["error"]
        for item in errors
    )

def _reconcile_snapshot(collection, dataset_id, schema, table, connection,
                        concurrently, source_entries, finish):
    doc = collection.find_one({"_id": dataset_id})
    if not doc:
        raise LookupError("Index target dataset does not exist")
    entries = doc.get("secondary_indexes") or []
    errors = []
    applied = 0
    with connection.cursor() as cursor:
        cursor.execute("SET lock_timeout = %s", ("5s",))
        cursor.execute("SET statement_timeout = %s", ("30min",))
        source_names = {e["index_name"] for e in source_entries}
        for entry in list(source_entries) + entries:
            source = entry in source_entries
            try:
                with advisory_lock(
                    connection, f'ddp-index:v1:name:{schema}:{entry["index_name"]}'
                ):
                    if not source:
                        if entry["index_name"] in source_names:
                            raise IndexConflict(
                                "Source-managed indexes are read-only through the secondary API"
                            )
                        latest = collection.find_one({"_id": dataset_id}) or {}
                        live = next(
                            (
                                e
                                for e in latest.get("secondary_indexes", [])
                                if e["id"] == entry["id"]
                            ),
                            None,
                        )
                        if not live or live["revision"] != entry["revision"]:
                            raise IndexConflict(
                                "Index intent changed before DDL; retry"
                            )
                    outcome = apply_entry(
                        cursor, entry, schema, table, concurrently=concurrently
                    )
                    if not source and not record_result(
                        collection, dataset_id, entry["id"], entry["revision"], outcome
                    ):
                        raise IndexConflict("Index intent changed during DDL; retry")
                    applied += 1
            except Exception as exc:
                errors.append({"index_name": entry["index_name"], "error": str(exc)})
                if not source:
                    try:
                        record_result(
                            collection,
                            dataset_id,
                            entry["id"],
                            entry["revision"],
                            {"status": "error", "last_error": str(exc)},
                        )
                    except Exception as recording_error:
                        errors.append(
                            {
                                "index_name": entry["index_name"],
                                "error": str(recording_error),
                            }
                        )
        if not errors:
            try:
                cursor.execute(f"ANALYZE {quote(schema)}.{quote(table)}")
            except Exception as exc:
                errors.append({"error": str(exc)})
    latest = collection.find_one({"_id": dataset_id}) or {}
    if latest.get("source_indexes") != doc.get("source_indexes"):
        errors.append(
            {"error": "Source index policy changed during reconciliation; retry"}
        )
    if _version(latest.get("secondary_indexes", [])) != _version(entries):
        errors.append(
            {"error": "Index definitions changed during reconciliation; retry"}
        )
    if errors:
        collection.update_one(
            {"_id": dataset_id},
            {
                "$set": {
                    "index_repair_pending": True,
                    "index_repair_error": _error_text(errors),
                    "indexes_applied": False,
                }
            },
        )
        result = ReconcileResult(
            False, applied, tuple(errors), latest.get("secondary_indexes_revision", 0)
        )
        raise IndexRepairError(_error_text(errors), result)
    # Never let index verification stand in for a data commit checkpoint.
    if finish and latest.get("index_data_ready") is not False:
        query = {
            "_id": dataset_id,
            "secondary_indexes_revision": expected_revision(latest),
            "index_data_token": latest.get("index_data_token"),
            "source_indexes": latest.get("source_indexes", {"$exists": False}),
        }
        changed = collection.update_one(
            query,
            {
                "$set": {
                    "index_repair_pending": False,
                    "index_repair_error": None,
                    "indexes_applied": True,
                    "index_repair_stage": "verified",
                }
            },
        )
        if not changed.matched_count:
            raise IndexRepairError(
                "Dataset changed before reconciliation completion; retry"
            )
    return ReconcileResult(
        True, applied, (), latest.get("secondary_indexes_revision", 0)
    )


def listing(collection, dataset_id, schema, table, connection, source_entries=()):
    doc = collection.find_one({"_id": dataset_id}) or {}
    entries = {e["index_name"]: e for e in doc.get("secondary_indexes", [])}
    sources = {e["index_name"]: e for e in source_entries}
    with connection.cursor() as cursor:
        physical = {p.index_name: p for p in list_indexes(cursor, schema, table)}
        # Show the same readiness in the UI that reconciliation checks.
        readiness = {name: index_ready(cursor, p) for name, p in physical.items()}
    result = []
    for name in sorted(set(entries) | set(sources) | set(physical)):
        entry, actual = entries.get(name), physical.get(name)
        kind = (
            "constraint"
            if actual and (actual.primary or actual.constraint_owned)
            else "source" if name in sources else "secondary" if entry else "unmanaged"
        )
        row = dict(
            entry or {},
            schema=schema,
            table=table,
            index_name=name,
            classification=kind,
            index_repair_pending=bool(doc.get("index_repair_pending")),
            index_repair_error=doc.get("index_repair_error"),
            physical_ready=readiness.get(name, False),
            physical_state=(
                "missing"
                if actual is None
                else "ready" if readiness[name] else "invalid"
            ),
            can_delete=kind == "secondary" and (actual is None or owns(entry, actual)),
        )
        if entry:
            row["target_sql"] = parse_index(entry["sql"]).render(schema, table)
        if actual:
            row["index_definition"] = actual.definition
            if entry and not owns(entry, actual):
                row.update(
                    physical_state="unmanaged", physical_ready=False, can_delete=False
                )
        result.append(row)
    return result

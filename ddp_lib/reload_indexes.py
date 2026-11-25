"""Transactional temporary removal for full loads; saved intent is never deleted."""

from dataclasses import dataclass, replace
import os

from ddp_lib.index_metadata import IndexConflict, expected_revision
from ddp_lib.index_sql import parse_index, quote
from ddp_lib.postgres_indexes import inspect_index
from ddp_lib import secondary_indexes as lifecycle


@dataclass(frozen=True)
class ReloadPreparation:
    token: str
    dropped: tuple


def prepare_reload(manager, target, connection, token):
    if connection.autocommit:
        raise ValueError("Temporary index removal requires the publication transaction")
    doc = target.collection.find_one({"_id": target.id}) or {}
    schema_key = "data_mart_schema" if target.kind == "REFERENCE" else "schema"
    if (
        not token
        or doc.get("index_data_token") != token
        or doc.get("index_data_ready") is not False
        or doc.get("table_name") != target.table
        or doc.get(schema_key, target.schema) != target.schema
    ):
        raise IndexConflict(
            "Temporary removal requires the current uncommitted data attempt"
        )
    target = replace(target, document=doc)
    with connection.cursor() as cursor:
        cursor.execute(
            "SET LOCAL lock_timeout = %s", (os.getenv("INDEX_LOCK_TIMEOUT", "5s"),)
        )
        cursor.execute(
            "SET LOCAL statement_timeout = %s",
            (os.getenv("INDEX_STATEMENT_TIMEOUT", "30min"),),
        )
        # Fence table/index DDL before catalog checks. Only an actual ordinary
        # DROP upgrades this to ACCESS EXCLUSIVE and blocks readers until commit.
        cursor.execute(
            f"LOCK TABLE {quote(target.schema)}.{quote(target.table)} IN SHARE ROW EXCLUSIVE MODE"
        )
        sources = manager.source_policy(target, connection)
        entries = sources + list(doc.get("secondary_indexes") or [])
        selected, names = [], set()
        for entry in entries:
            definition = parse_index(entry["sql"])
            if (
                entry["index_name"] != definition.index_name
                or entry["index_name"] in names
            ):
                raise IndexConflict("Duplicate or mismatched managed index name")
            names.add(entry["index_name"])
            if entry["desired_state"] not in {"present", "absent"}:
                raise IndexConflict("Invalid desired index state")
            if entry["desired_state"] == "absent":
                continue  # Tombstones remain authoritative during restoration.
            # Validate *all* desired definitions, including currently missing ones.
            expected = lifecycle.canonical_signature(
                cursor, definition, target.schema, target.table
            )
            physical = inspect_index(cursor, target.schema, entry["index_name"])
            if physical is None:
                continue
            if (physical.schema, physical.table) != (target.schema, target.table):
                raise IndexConflict("Index name is occupied by another relation")
            if (
                physical.primary
                or physical.constraint_owned
                or physical.relation_kind == "p"
            ):
                continue
            cursor.execute(
                "SELECT 1 FROM pg_inherits WHERE inhrelid=%s OR inhparent=%s",
                (physical.oid, physical.oid),
            )
            if cursor.fetchone():
                continue
            lifecycle.validate_existing(
                cursor, entry, physical, target.schema, target.table
            )
            if lifecycle.catalog_signature(physical, definition) != expected:
                raise IndexConflict("Managed index differs from its saved definition")
            selected.append((entry, physical))

        query = {
            "_id": target.id,
            "index_data_token": token,
            "index_data_ready": False,
            "secondary_indexes_revision": expected_revision(doc),
            "source_indexes": doc.get("source_indexes", {"$exists": False}),
        }
        # Acknowledged durable definitions and resolved source names precede the
        # first DROP. No metadata change may convert this to an absent intent.
        snapshot = {
            "token": token,
            "schema": target.schema,
            "table": target.table,
            "sources": sources,
            "secondary_revision": doc.get("secondary_indexes_revision", 0),
            "indexes": [
                dict(entry, physical_definition=p.definition, comment=p.comment)
                for entry, p in selected
            ],
        }
        if not target.collection.update_one(
            query,
            {
                "$set": {
                    "index_reload": snapshot,
                    "index_repair_pending": True,
                    "index_repair_error": None,
                    "indexes_applied": False,
                }
            },
        ).matched_count:
            raise IndexConflict(
                "Index definitions changed during reload preparation; retry"
            )
        for entry, physical in selected:
            if not target.collection.find_one(query):
                raise IndexConflict(
                    "Index definitions changed before temporary removal; retry"
                )
            cursor.execute(
                f"DROP INDEX {quote(physical.schema)}.{quote(physical.index_name)} RESTRICT"
            )
        return ReloadPreparation(token, tuple(p.index_name for _, p in selected))

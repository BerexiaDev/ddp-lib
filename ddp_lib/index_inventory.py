"""I06 observation, offline review and metadata-only index migration.

No source connection, SQL normalization via temporary DDL, or reconciliation is
performed here. Operators must stop incompatible writers for the entire cutover;
MongoDB and PostgreSQL do not share a transaction.
"""

from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from datetime import datetime, timezone

from ddp_lib.dataset_indexes import Dataset, DatasetIndexes, KINDS, source_entries
from ddp_lib.enums import DataMartSchemas
from ddp_lib.index_metadata import IndexConflict, save_intent
from ddp_lib.index_sql import parse_index, quote
from ddp_lib.postgres_indexes import list_indexes


FORMAT = "ddp-index-inventory"
POLICY = "preserve-existing"
ENGINES = frozenset(
    {"postgres", "sqlserver", "oracle", "informix", "mysql", "mongo", "db2i", "sftp"}
)
PUBLIC_CONNECTOR_FIELDS = ("type", "host", "port", "database", "schema", "user")
CONNECTOR_LABELS = (
    "_id",
    "name",
    "created_on",
    "modified_on",
    "created_at",
    "updated_at",
)
DOCUMENT_FIELDS = (
    "_id",
    "name",
    "status",
    "table_name",
    "schema",
    "data_mart_schema",
    "source_schema",
    "source_table_name",
    "connector_id",
    "is_imported_file",
    "source_indexes",
    "indexes",
    "query_indexes",
    "secondary_indexes",
    "secondary_indexes_revision",
    "legacy_indexes_retired",
)
NESTED_FIELDS = {
    "source_indexes": ("name", "columns", "unique", "primary", "source_idxtype"),
    "query_indexes": ("sql", "normalized_sql", "schema", "table", "index_name"),
    "secondary_indexes": (
        "id",
        "schema",
        "table",
        "index_name",
        "sql",
        "desired_state",
        "revision",
    ),
}


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def schema_defaults():
    return {member.name: member.value for member in DataMartSchemas}


def environment_identity(database, connection):
    # No credentials, connection strings, or private setting fingerprints.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT current_database(), inet_server_addr()::text, inet_server_port()"
        )
        name, address, port = cursor.fetchone()
    return dict(
        metadata_database=database.name,
        datamart_database=name,
        datamart_address=address,
        datamart_port=port,
    )


class InventoryError(ValueError):
    """Stable diagnostic codes; never embed driver errors/connection settings."""


@dataclass(frozen=True)
class ImportResult:
    applied: int
    skipped: int
    retired: int

    def to_dict(self):
        return asdict(self)


@contextmanager
def catalog_session(connection):
    """Enforce read-only catalog operations, including during import locks."""
    if not connection.autocommit:
        raise InventoryError("dedicated_autocommit_connection_required")
    previous = connection.readonly
    connection.set_session(readonly=True)
    try:
        yield
    finally:
        if not connection.closed:
            connection.set_session(readonly="DEFAULT" if previous is None else previous)


def explicit_schema(document):
    schema = document.get("data_mart_schema")
    if not schema:
        raise InventoryError("unresolved_destination_identity")
    return schema


def document_evidence(document):
    """Exclude arbitrary embedded connector joins and nested settings."""
    result = {
        key: deepcopy(document[key]) for key in DOCUMENT_FIELDS if key in document
    }
    for key, fields in NESTED_FIELDS.items():
        if isinstance(result.get(key), list):
            result[key] = [
                (
                    {
                        field: deepcopy(value[field])
                        for field in fields
                        if field in value
                    }
                    if isinstance(value, dict)
                    else None
                )
                for value in result[key]
            ]
    return result


def connector_context(document, connector):
    if not connector:
        return None
    context = {
        key: connector.get(key) for key in ("_id", "name") + PUBLIC_CONNECTOR_FIELDS
    }
    # Core source_schema is an override; legacy schema is the source alias,
    # never a destination substitute. Engine defaults are only observations.
    context["schema"] = (
        document.get("source_schema")
        or document.get("schema")
        or connector.get("schema")
    )
    if context["schema"] is None:
        context["schema"] = {
            "postgres": "public",
            "sqlserver": "dbo",
            "mysql": connector.get("database"),
            "oracle": connector.get("user"),
            "informix": connector.get("user"),
        }.get(connector.get("type"))
    return context


def compare_connectors(owners, connectors):
    """Compare private values in memory, export only tri-state equality."""
    result = dict(
        classification="unresolved_or_mixed_owners",
        matching_public_settings=None,
        matching_passwords=None,
        matching_other_settings=None,
        differing_public_fields=[],
    )
    if any(o["dataset_type"] != "REFERENCE" or not o.get("connector") for o in owners):
        return result
    contexts = [o["connector"] for o in owners]
    settings = [connectors.get(o.get("connector_id")) for o in owners]
    if not all(settings):
        return result
    differing = [
        key
        for key in PUBLIC_CONNECTOR_FIELDS
        if any(c.get(key) != contexts[0].get(key) for c in contexts[1:])
    ]
    incomplete = any(
        c.get(key) in (None, "")
        for c in contexts
        for key in PUBLIC_CONNECTOR_FIELDS
        if key != "schema" or c.get("type") not in {"mongo", "sftp"}
    )
    public = False if differing else None if incomplete else True
    passwords = [c.get("password") for c in settings]
    matching_passwords = (
        all(p == passwords[0] for p in passwords)
        if all(isinstance(p, str) for p in passwords)
        else None
    )
    # Include every opaque setting (protocol, locale, TLS, URLs, key files,
    # tokens and future connector options). Never export keys or their values.
    private = [
        {
            key: value
            for key, value in c.items()
            if value is not None
            and key not in PUBLIC_CONNECTOR_FIELDS + CONNECTOR_LABELS + ("password",)
        }
        for c in settings
    ]
    matching_private = all(p == private[0] for p in private) if any(private) else None
    if public is False or matching_private is False:
        classification = "different_connector_settings"
    elif public is None:
        classification = "incomplete_connector_settings"
    elif matching_passwords is False:
        classification = "same_endpoint_different_credentials"
    elif matching_passwords is None:
        classification = "same_endpoint_credentials_unknown"
    elif matching_private is None:
        classification = "same_endpoint_private_settings_unknown"
    else:
        classification = "duplicate_connector_settings"
    result.update(
        classification=classification,
        matching_public_settings=public,
        matching_passwords=matching_passwords,
        matching_other_settings=matching_private,
        differing_public_fields=differing,
    )
    return result


def read_datasets(database):
    datasets, connectors = [], {}
    for kind, collection in KINDS.items():
        for doc in database[collection].find({}):
            issues = []
            schema = doc.get("data_mart_schema" if kind == "REFERENCE" else "schema")
            table = doc.get("table_name")
            try:
                quote(schema)
                quote(table)
            except ValueError:
                issues.append("unresolved_destination_identity")
                schema = None
            if not isinstance(doc["_id"], str) or not doc["_id"]:
                issues.append("unsupported_dataset_id")
            connector = None
            if kind == "REFERENCE" and not doc.get("is_imported_file"):
                connector_id = doc.get("connector_id")
                connector = (
                    database["connectors"].find_one({"_id": connector_id})
                    if connector_id
                    else None
                )
                if connector is None:
                    issues.append("unresolved_connector")
                else:
                    connectors[connector_id] = connector
                    if connector.get("type") not in ENGINES:
                        issues.append("unsupported_connector_type")
            evidence = document_evidence(doc)
            target = Dataset(kind, database[collection], doc, schema, table)
            if schema:
                try:
                    source_entries(target)
                except (ValueError, KeyError, TypeError):
                    issues.append("unresolved_source_policy")
            datasets.append(
                dict(
                    dataset_type=kind,
                    collection=collection,
                    id=doc["_id"],
                    name=doc.get("name"),
                    status=doc.get("status"),
                    schema=schema,
                    table=table,
                    connector_id=doc.get("connector_id"),
                    connector=connector_context(doc, connector),
                    document=evidence,
                    issues=issues,
                )
            )
    return datasets, connectors


def relation_inventory(connection, schema, table):
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT c.oid, c.relkind, c.relispartition FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE n.nspname=%s AND c.relname=%s",
            (schema, table),
        )
        row = cursor.fetchone()
        relation = dict(
            schema=schema,
            table=table,
            exists=row is not None,
            oid=row[0] if row else None,
            relation_kind=row[1] if row else None,
            is_partition=row[2] if row else False,
            columns=[],
            indexes=[],
        )
        if row:
            cursor.execute(
                "SELECT attname, format_type(atttypid, atttypmod) FROM pg_attribute "
                "WHERE attrelid=%s AND attnum>0 AND NOT attisdropped ORDER BY attnum",
                (row[0],),
            )
            relation["columns"] = [
                dict(name=name, data_type=data_type)
                for name, data_type in cursor.fetchall()
            ]
            for actual in list_indexes(cursor, schema, table):
                if actual is None:
                    raise InventoryError("catalog_changed_during_inventory")
                index = asdict(actual)
                # Arbitrary comments are not required evidence and may contain secrets.
                index["ownership_receipt"] = any(
                    line.startswith("[ddp-index:v1:")
                    for line in (index.pop("comment") or "").splitlines()
                )
                relation["indexes"].append(index)
        return relation


def owners_for(datasets, schema, table):
    # An unresolved record with this table name could own this very relation.
    # It blocks adoption until its explicit identity has been repaired.
    return [
        d for d in datasets if d["table"] == table and d["schema"] in (schema, None)
    ]


def exclusion(item, datasets, relation):
    owners = owners_for(datasets, item["schema"], item["table"])
    if len(owners) != 1 or (owners[0]["collection"], owners[0]["id"]) != (
        item["collection"],
        item["dataset_id"],
    ):
        return "unresolved_or_duplicate_owner"
    owner = owners[0]
    if owner["issues"]:
        return owner["issues"][0]
    doc = owner["document"]
    if any(
        e["index_name"] == item["index_name"]
        for e in doc.get("secondary_indexes") or []
    ):
        return "existing_registry_entry_or_tombstone"
    actual = next(
        (
            i
            for i in (relation or {}).get("indexes", [])
            if i["index_name"] == item["index_name"]
        ),
        None,
    )
    if not actual:
        return "missing_legacy_index"
    if actual["primary"] or actual["constraint_owned"]:
        return "constraint_owned"
    if actual["relation_kind"] not in ("r", "m") or relation.get("is_partition"):
        return "partitioned_or_unsupported_relation"
    if not actual["valid"] or not actual["ready"]:
        return "invalid_or_not_ready"
    try:
        definition = parse_index(actual["definition"])
        if (definition.schema, definition.table, definition.index_name) != (
            item["schema"],
            item["table"],
            item["index_name"],
        ):
            return "catalog_identity_mismatch"
        sources = source_entries(
            Dataset(owner["dataset_type"], None, doc, owner["schema"], owner["table"])
        )
        if any(
            s["index_name"] == item["index_name"]
            or parse_index(s["sql"]).signature == definition.signature
            for s in sources
        ):
            return "source_policy"
    except (ValueError, KeyError, TypeError):
        return "unresolved_source_policy_or_definition"
    if actual["ownership_receipt"]:
        return "existing_physical_ownership_receipt"
    return None


def extract_inventory(database, connection):
    """Read fresh destination state. A shared physical relation is inspected once."""
    started = timestamp()
    datasets, connectors = read_datasets(database)
    relations, conflicts, items, issues = {}, [], {}, []
    targets = {(d["schema"], d["table"]) for d in datasets if d["schema"]}
    with catalog_session(connection):
        environment = environment_identity(database, connection)
        for schema, table in sorted(targets):
            relation = relation_inventory(connection, schema, table)
            relations[schema, table] = relation
            owners = owners_for(datasets, schema, table)
            if len(owners) != 1:
                conflicts.append(
                    dict(
                        schema=schema,
                        table=table,
                        owners=[
                            {
                                k: o[k]
                                for k in (
                                    "dataset_type",
                                    "collection",
                                    "id",
                                    "connector_id",
                                    "connector",
                                )
                            }
                            for o in owners
                        ],
                        connector_comparison=compare_connectors(owners, connectors),
                    )
                )
                issues.append(dict(code="duplicate_owner", schema=schema, table=table))
            if not relation["exists"]:
                issues.append(dict(code="missing_relation", schema=schema, table=table))
            for index in relation["indexes"]:
                owner = owners[0] if len(owners) == 1 else None
                item = dict(
                    collection=owner["collection"] if owner else None,
                    dataset_id=owner["id"] if owner else None,
                    schema=schema,
                    table=table,
                    index_name=index["index_name"],
                    sql=index["definition"],
                    approved=False,
                    origins=[{"type": "catalog"}],
                    issues=[],
                )
                items[schema, index["index_name"]] = item

    for dataset in datasets:
        issues.extend(
            dict(code=code, collection=dataset["collection"], id=dataset["id"])
            for code in dataset["issues"]
        )
        doc = dataset["document"]
        legacy = list(doc.get("query_indexes") or [])
        # Column choices identify only a conventional name, not a recoverable
        # lost definition. They can adopt live SQL but can never authorize CREATE.
        for column in doc.get("indexes") or []:
            if dataset["schema"] and isinstance(column, str):
                name = f"idx_{dataset['schema']}_{dataset['table']}_{column}".encode()[
                    :63
                ].decode("utf8", errors="ignore")
                legacy.append(
                    dict(
                        index_name=name,
                        schema=dataset["schema"],
                        table=dataset["table"],
                    )
                )
        for old in legacy:
            origin = dict(
                type="legacy", collection=dataset["collection"], id=dataset["id"]
            )
            try:
                sql = old.get("sql") or old.get("normalized_sql")
                definition = parse_index(sql) if sql else None
                schema, table, name = (
                    (definition.schema, definition.table, definition.index_name)
                    if definition
                    else (old["schema"], old["table"], old["index_name"])
                )
                for key, value in (
                    ("schema", schema),
                    ("table", table),
                    ("index_name", name),
                ):
                    quote(value)
                    if old.get(key) is not None and old[key] != value:
                        raise ValueError("identity mismatch")
            except (ValueError, KeyError, TypeError, AttributeError):
                issues.append(dict(code="invalid_legacy_definition", origin=origin))
                continue
            owners = owners_for(datasets, schema, table)
            owner = owners[0] if len(owners) == 1 else None
            key = (schema, name)
            if key not in items:
                items[key] = dict(
                    collection=owner["collection"] if owner else None,
                    dataset_id=owner["id"] if owner else None,
                    schema=schema,
                    table=table,
                    index_name=name,
                    sql=sql,
                    approved=False,
                    origins=[],
                    issues=["missing_legacy_index"],
                )
                issues.append(
                    dict(
                        code="missing_legacy_index",
                        schema=schema,
                        table=table,
                        index_name=name,
                        origin=origin,
                    )
                )
            item = items[key]
            item["origins"].append(origin)
            if item["table"] != table or (
                definition
                and item.get("sql")
                and parse_index(item["sql"]).signature != definition.signature
            ):
                item["issues"].append("mismatched_definition")
                issues.append(
                    dict(
                        code="mismatched_definition",
                        schema=schema,
                        table=table,
                        index_name=name,
                        origin=origin,
                    )
                )

    for item in items.values():
        item["exclusion"] = exclusion(
            item, datasets, relations.get((item["schema"], item["table"]))
        )
    return dict(
        format=FORMAT,
        version=2,
        mode="extraction",
        extraction_started_at=started,
        extraction_completed_at=timestamp(),
        schema_defaults=schema_defaults(),
        environment=environment,
        datasets=datasets,
        relations=list(relations.values()),
        ownership_conflicts=conflicts,
        items=list(items.values()),
        issues=issues,
        summary=dict(
            datasets=len(datasets),
            physical_targets=len(relations),
            physical_indexes=sum(len(r["indexes"]) for r in relations.values()),
            missing_relations=sum(not r["exists"] for r in relations.values()),
            ownership_conflicts=len(conflicts),
            candidates=len(items),
        ),
    )


def check_format(report):
    if report.get("format") != FORMAT or report.get("version") != 2:
        raise InventoryError(
            "unsupported_inventory_format_extract_fresh_core_inventory"
        )


def prepare_inventory(report):
    """Offline copy; preserve observations/issues and explain every selection."""
    check_format(report)
    if report.get("mode") != "extraction":
        raise InventoryError("prepare_requires_original_extraction")
    reviewed = deepcopy(report)
    reviewed.update(mode="review", migration_policy=POLICY, prepared_at=timestamp())
    relations = {(r["schema"], r["table"]): r for r in reviewed["relations"]}
    for item in reviewed["items"]:
        relation = relations.get((item["schema"], item["table"]))
        reason = exclusion(item, reviewed["datasets"], relation)
        item["approved"] = reason is None
        item["review"] = reason or "selected_existing_secondary"
        if reason is None:
            # Explicitly selecting existing SQL may supersede mismatched legacy
            # SQL; the original mismatch remains recorded in both issue lists.
            item["sql"] = next(
                i["definition"]
                for i in relation["indexes"]
                if i["index_name"] == item["index_name"]
            )
    # Retire even populations with no selected indexes, otherwise a missing
    # legacy choice could be auto-created by their next rebuild.
    reviewed["legacy_retirements"] = [
        dict(collection=d["collection"], dataset_id=d["id"], approved=True)
        for d in reviewed["datasets"]
        if d["dataset_type"] == "POPULATION"
    ]
    reviewed["review_counts"] = dict(
        Counter(item["review"] for item in reviewed["items"])
    )
    return reviewed


def current_owner(item, database):
    if item.get("collection") not in KINDS.values() or not isinstance(
        item.get("dataset_id"), str
    ):
        raise InventoryError("invalid_dataset_identity")
    datasets, _ = read_datasets(database)
    owners = owners_for(datasets, item["schema"], item["table"])
    if len(owners) != 1 or (owners[0]["collection"], owners[0]["id"]) != (
        item["collection"],
        item["dataset_id"],
    ):
        raise InventoryError("target_ownership_changed_or_unresolved")
    owner = owners[0]
    if owner["issues"]:
        raise InventoryError(owner["issues"][0])
    return owner


def _reload_unfinished(doc):
    """True while a bulk reload may still hold temporarily removed indexes.

    ddp-lib 0.3.0 left the snapshot in place after a successful reload, so a
    snapshot for the current data token whose indexes are verified is finished.
    """
    reload = doc.get("index_reload")
    if not reload:
        return False
    return not (
        reload.get("token") == doc.get("index_data_token")
        and doc.get("index_repair_stage") == "verified"
        and not doc.get("index_repair_pending")
    )


def validate_import(item, database, connection):
    owner = current_owner(item, database)
    doc = database[item["collection"]].find_one({"_id": item["dataset_id"]})
    target = Dataset(
        owner["dataset_type"],
        database[item["collection"]],
        doc,
        owner["schema"],
        owner["table"],
    )
    # Existing intent wins even if its old physical object has since disappeared.
    if any(
        e["index_name"] == item["index_name"]
        for e in doc.get("secondary_indexes") or []
    ):
        return target, None
    definition = parse_index(item["sql"])
    if (definition.schema, definition.table, definition.index_name) != (
        item["schema"],
        item["table"],
        item["index_name"],
    ):
        raise InventoryError("inventory_sql_identity_changed")
    if doc.get("index_data_ready") is False or _reload_unfinished(doc):
        raise InventoryError("incomplete_data_attempt_requires_recovery")
    relation = relation_inventory(connection, item["schema"], item["table"])
    reason = exclusion(item, [owner], relation)
    if reason:
        raise InventoryError(reason)
    actual = next(
        i for i in relation["indexes"] if i["index_name"] == item["index_name"]
    )
    if parse_index(actual["definition"]).signature != definition.signature:
        raise InventoryError("catalog_definition_changed")
    return target, actual


def validate_retirement(item, report, database):
    if item.get("collection") != "populations" or not isinstance(
        item.get("dataset_id"), str
    ):
        raise InventoryError("invalid_legacy_retirement")
    evidence = next(
        (
            d
            for d in report["datasets"]
            if d["collection"] == item["collection"] and d["id"] == item["dataset_id"]
        ),
        None,
    )
    doc = database["populations"].find_one({"_id": item["dataset_id"]})
    if not doc or not evidence:
        raise InventoryError("legacy_retirement_dataset_changed")
    if not doc.get("legacy_indexes_retired"):
        current = document_evidence(doc)
        if any(
            current.get(k) != evidence["document"].get(k)
            for k in ("query_indexes", "indexes", "schema", "table_name")
        ):
            raise InventoryError("legacy_configuration_changed")
    # Retirement never authorizes a physical target, but uses the same dataset
    # lock. An unresolved target cannot be safely locked using Core's contract.
    quote(doc.get("schema"))
    quote(doc.get("table_name"))
    return Dataset(
        "POPULATION", database["populations"], doc, doc["schema"], doc["table_name"]
    )


def apply_inventory(report, database, connection):
    """Preflight the complete plan, then recheck under Core locks; no SQL DDL."""
    check_format(report)
    if report.get("mode") != "review" or report.get("migration_policy") != POLICY:
        raise InventoryError("only_reviewed_existing_policy_supported")
    if report.get("schema_defaults") != schema_defaults():
        raise InventoryError("schema_defaults_changed")
    selected = [i for i in report["items"] if i.get("approved") is True]
    retirements = [
        i for i in report.get("legacy_retirements", []) if i.get("approved") is True
    ]
    keys = [(i["schema"], i["index_name"]) for i in selected]
    if len(set(keys)) != len(keys):
        raise InventoryError("duplicate_selected_index")
    # A partially edited plan must not silently leave legacy replay enabled.
    required = {
        d["id"] for d in report["datasets"] if d["dataset_type"] == "POPULATION"
    }
    if {i["dataset_id"] for i in retirements} != required:
        raise InventoryError("all_population_legacy_retirements_must_be_reviewed")
    manager = DatasetIndexes(database, None, explicit_schema)
    applied, skipped, retired = 0, len(report["items"]) - len(selected), 0
    with catalog_session(connection):
        if report.get("environment") != environment_identity(database, connection):
            raise InventoryError("destination_environment_changed")
        for item in selected:
            validate_import(item, database, connection)
        for item in retirements:
            validate_retirement(item, report, database)
        for item in selected:
            target, _ = validate_import(item, database, connection)
            with manager.guard(target, connection):
                target, actual = validate_import(item, database, connection)
                if actual is None:
                    skipped += 1
                    continue
                # Do not mark this as physically created. Reconciliation must
                # verify and stamp the adoption before clearing repair flags.
                save_intent(
                    target.collection,
                    target.id,
                    target.schema,
                    target.table,
                    actual["definition"],
                    {"type": "inventory", "specification": "I06"},
                    adopt=True,
                    only_if_missing=True,
                )
                applied += 1
        for item in retirements:
            target = validate_retirement(item, report, database)
            with manager.guard(target, connection):
                target = validate_retirement(item, report, database)
                doc = target.document
                if doc.get("legacy_indexes_retired") is True:
                    continue
                query = {"_id": target.id}
                for key in (
                    "schema",
                    "table_name",
                    "query_indexes",
                    "indexes",
                    "legacy_indexes_retired",
                ):
                    query[key] = (
                        {"$eq": doc[key], "$exists": True}
                        if key in doc
                        else {"$exists": False}
                    )
                result = target.collection.update_one(
                    query, {"$set": {"legacy_indexes_retired": True}}
                )
                if not result.matched_count:
                    raise IndexConflict("legacy_configuration_changed")
                retired += 1
    return ImportResult(applied, skipped, retired)

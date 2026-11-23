"""Client-neutral target registry and lifecycle integration for Core datasets."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from ddp_lib import secondary_indexes as lifecycle
from ddp_lib.enums import IndexCreationMode
from ddp_lib.index_metadata import save_intent, delete_intent, IndexConflict
from ddp_lib.index_sql import parse_index, quote
from ddp_lib.postgres_indexes import advisory_lock, dataset_session, inspect_index

KINDS = {'REFERENCE': 'references', 'POPULATION': 'populations', 'CLEANED_TABLE': 'cleaned_tables'}
DEPENDENCIES = {'tables_ids': 'REFERENCE', 'population_ids': 'POPULATION', 'cleaned_table_ids': 'CLEANED_TABLE'}
_ANY_DATA_TOKEN = object()


@dataclass
class Dataset:
    kind: str
    collection: object
    document: dict
    schema: str
    table: str

    @property
    def id(self):
        return self.document['_id']


def source_entries(dataset, physical=()):
    """Turn a reference table's saved index list into indexes DDP should keep.

    The name can change. The identity is the column list, in order, plus unique or not.
    An older index can be kept only when it has the same columns and is not a secondary index.
    An older index without a DDP stamp still needs an explicit adoption.
    """
    # Populations and cleaned tables do not use this list.
    if dataset.kind != "REFERENCE":
        return []
    policies = dataset.document.get("source_indexes")
    if policies is None:
        policies = []
    if not isinstance(policies, list):
        raise ValueError("Source index policy must be a list; review source metadata")
    candidates, names = {}, {}
    # Names already saved as secondary indexes. A source index must not reuse one.
    excluded = {e["index_name"] for e in dataset.document.get("secondary_indexes", [])}
    for policy in policies:
        if not isinstance(policy, dict):
            raise ValueError("Source index definition must be an object")
        # source_idxtype only records the old Informix index type. It is not used here.
        unsupported = set(policy) - {
            "name",
            "columns",
            "unique",
            "primary",
            "source_idxtype",
        }
        if unsupported:
            raise ValueError(
                "Unsupported source index fields: " + ", ".join(sorted(unsupported))
            )
        if any(
            key in policy and not isinstance(policy[key], bool)
            for key in ("unique", "primary")
        ):
            raise ValueError("Source index unique/primary flags must be booleans")
        # PostgreSQL already owns the primary key.
        if policy.get("primary"):
            continue
        columns = policy.get("columns")
        if (
            not isinstance(columns, list)
            or not columns
            or any(not isinstance(c, str) or not c.strip() for c in columns)
        ):
            raise ValueError(
                "Source index policy has unsupported keys; review source metadata"
            )
        for column in columns:
            quote(column)  # Check the column name. Do not change its text.
        unique = bool(policy.get("unique"))

        # Build a lowercase index name: idx_<schema>_<table>_<columns>.
        slug = lambda s: re.sub("_+", "_", re.sub("[^a-z0-9_]+", "_", s.lower())).strip(
            "_"
        )
        name = "idx_" + "_".join(
            [slug(dataset.schema), slug(dataset.table)] + [slug(c) for c in columns]
        )
        if unique:
            name += "_uniq"
        identity = json.dumps([dataset.kind, str(dataset.id), columns, unique])
        candidates[identity] = (name, columns, unique)
        names.setdefault(name, set()).add(identity)
    result = []
    used = set()
    for identity, (name, columns, unique) in candidates.items():
        # PostgreSQL allows 63 bytes. Shared short names get a fixed ending too.
        if len(name.encode()) > 63 or len(names[name]) > 1:
            name = name[:49] + "_" + sha256(identity.encode()).hexdigest()[:12]
        sql = (
            "CREATE "
            + ("UNIQUE " if unique else "")
            + "INDEX "
            + quote(name)
            + " ON "
            + quote(dataset.schema)
            + "."
            + quote(dataset.table)
            + " ("
            + ", ".join(quote(c) for c in columns)
            + ")"
        )
        entry = {
            "id": "source:" + sha256(identity.encode()).hexdigest(),
            "index_name": name,
            "sql": sql,
            "desired_state": "present",
            "adopt": False,
            "revision": 1,
        }
        definition = parse_index(sql)
        if name in excluded:
            raise IndexConflict(
                f"Source index name {name!r} is already owned by secondary intent"
            )
        # Keep an existing index only when the columns match and it is not a constraint.
        # A DDP stamp is accepted only when this same source entry owns it.
        matches = [
            p
            for p in physical
            if p.index_name not in excluded | used
            and not p.primary
            and not p.constraint_owned
            and (p.schema, p.table) == (dataset.schema, dataset.table)
            and lifecycle.catalog_signature(p, definition) == definition.signature
            and (
                not any(
                    line.startswith("[ddp-index:v1:")
                    for line in (p.comment or "").splitlines()
                )
                or lifecycle.owns(entry, p)
            )
        ]
        # Use the new name when it exists. Otherwise use a ready older index with the same columns.
        actual = next(
            iter(
                sorted(
                    matches,
                    key=lambda p: (
                        p.index_name != name,
                        not (p.valid and p.ready),
                        p.index_name,
                    ),
                )
            ),
            None,
        )
        if actual:
            name = actual.index_name
            sql = definition.render(name=name, concurrently=False)
        else:
            # Temporary removal must not forget a previously bound source name.
            # Current policy identities win: this cannot resurrect deleted policy.
            recovery = dataset.document.get("index_reload") or {}
            if (recovery.get("schema"), recovery.get("table")) == (
                dataset.schema,
                dataset.table,
            ):
                saved = next(
                    (e for e in recovery.get("sources", []) if e["id"] == entry["id"]),
                    None,
                )
                if (
                    saved
                    and parse_index(saved["sql"]).signature == definition.signature
                ):
                    name = saved["index_name"]
                    sql = definition.render(name=name, concurrently=False)
        if name in excluded:
            raise IndexConflict(
                "Recovered source index name is owned by secondary intent"
            )
        if name in used:
            raise IndexConflict("Source index names collide; review source policy")
        used.add(name)
        result.append(dict(entry, index_name=name, sql=sql))
    return result


class DatasetIndexes:
    def __init__(self, database, connector_factory, resolve_reference):
        self.database = database
        self.connector_factory = connector_factory
        self.resolve_reference = resolve_reference

    def get(self, kind, dataset_id, *, reference_schema=None):
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError('dataset_id must be a non-empty string')
        kind = str(kind).upper().replace(' ', '_')
        if kind not in KINDS:
            raise ValueError('dataset_type must be REFERENCE, POPULATION or CLEANED_TABLE')
        collection = self.database[KINDS[kind]]
        doc = collection.find_one({'_id': dataset_id})
        if not doc:
            raise LookupError('Dataset not found')
        if kind == 'REFERENCE' and reference_schema and not doc.get('data_mart_schema'):
            doc['data_mart_schema'] = reference_schema
        schema = self.resolve_reference(doc) if kind == 'REFERENCE' else doc.get('schema')
        table = doc.get('table_name')
        quote(schema)
        quote(table)
        target = Dataset(kind, collection, doc, schema, table)
        owners = []
        for candidate_kind, name in KINDS.items():
            key = 'data_mart_schema' if candidate_kind == 'REFERENCE' else 'schema'
            owners.extend((name, d['_id']) for d in self.database[name].find({key: schema, 'table_name': table}))
        # A legacy reference identity may still be in the worker result buffer.
        # Its verified identity is already authoritative
        # for this attempt even before the ordinary result is projected.
        if (collection.name, dataset_id) not in owners:
            owners.append((collection.name, dataset_id))
        if len(owners) != 1:
            raise IndexConflict('Duplicate or unresolved physical dataset owners; run the KAN-20 migration audit')
        return target

    def inputs(self, document):
        targets = {}
        for field, kind in DEPENDENCIES.items():
            for identifier in document.get(field) or []:
                target = self.get(kind, identifier)
                targets[(kind, identifier)] = target
        return list(targets.values())

    def population_targets(self, population_id):
        target = self.get('POPULATION', population_id)
        return [target] + self.inputs(target.document)

    def resolve(self, schema, table, allowed):
        matches = [d for d in allowed if (d.schema, d.table) == (schema, table)]
        if len(matches) != 1:
            raise ValueError('SQL target must be exactly one registered dataset dependency')
        return matches[0]

    def factory(self, target):
        return self.connector_factory(target.schema).get_connection

    @contextmanager
    def guard(self, target, connection=None):
        if connection is not None:
            if not connection.autocommit:
                raise ValueError('Index DDL requires a dedicated autocommit connection')
            with advisory_lock(connection, f'ddp-index:v1:dataset:{target.collection.full_name}:{target.id}'):
                with advisory_lock(connection, f'ddp-index:v1:relation:{target.schema}:{target.table}'):
                    yield connection
            return
        with dataset_session(self.factory(target), target.collection.full_name, target.id,
                             target.schema, target.table) as connection:
            yield connection

    def source_policy(self, target, connection):
        from ddp_lib.postgres_indexes import list_indexes
        with connection.cursor() as cursor:
            physical = list_indexes(cursor, target.schema, target.table)
        return source_entries(target, physical)

    def validate_source_policy(self, target, connection):
        """Reject unsupported saved keys before the data writer changes live data."""
        entries = source_entries(target)
        # Check definitions on an empty temporary table, without touching live rows.
        with connection.cursor() as cursor:
            for entry in entries:
                lifecycle.canonical_signature(cursor, parse_index(entry['sql']), target.schema, target.table)

    def prepare_reload(self, target, connection, token):
        """Prepare temporary removal on the caller's publication transaction.

        The data writer already holds the dataset guard. Rollback must cover
        these DROPs, the replacement rows, and its SQL publication receipt.
        """
        from ddp_lib.reload_indexes import prepare_reload

        with self.guard(target):
            return prepare_reload(self, target, connection, token)

    def reconcile(self, target, connection=None, *, finish=True, mode=IndexCreationMode.ONLINE.value):
        """Verify indexes online, or restore them in a post-load maintenance window.

        Post-load CREATE may block target writes. It requires an existing data
        commit checkpoint; selecting this mode never creates that checkpoint.
        """
        if mode not in {item.value for item in IndexCreationMode}:
            raise ValueError('Index creation mode must be an IndexCreationMode value')
        # Keep the already resolved KAN-11 identity across lease expiry after a
        # reserved SQL commit; ordinary identity backfill may still be buffered.
        from dataclasses import replace

        document = target.collection.find_one({"_id": target.id})
        schema_field = "data_mart_schema" if target.kind == "REFERENCE" else "schema"
        try:
            with self.guard(target, connection) as connection:
                # Borrowed connections must also lock before reading the mode's
                # checkpoint: a data attempt may have started since get().
                document = target.collection.find_one({"_id": target.id})
                if (
                    not document
                    or document.get("table_name") != target.table
                    or document.get(schema_field, target.schema) != target.schema
                ):
                    raise IndexConflict("Dataset identity changed; retry")
                target = replace(target, document=document)
                if mode == IndexCreationMode.POST_LOAD.value and not (
                    document.get("index_data_ready") is True
                    and document.get("index_data_token")
                ):
                    raise lifecycle.IndexRepairError(
                        "Post-load indexes require a committed data checkpoint"
                    )
                return lifecycle.reconcile(
                    target.collection,
                    target.id,
                    target.schema,
                    target.table,
                    self.factory(target),
                    connection,
                    concurrently=mode == IndexCreationMode.ONLINE.value,
                    source_entries=self.source_policy(target, connection),
                    finish=finish,
                )
        except Exception as exc:
            target.collection.update_one(
                {
                    "_id": target.id,
                    "index_data_token": (document or {}).get("index_data_token"),
                },
                {
                    "$set": {
                        "index_repair_pending": True,
                        "index_repair_error": str(exc),
                        "indexes_applied": False,
                    }
                },
            )
            raise

    def repair(self, target, *, expected_token=_ANY_DATA_TOKEN):
        """Retry committed data in post-load mode; manual repair stays online."""
        with self.guard(target) as connection:
            document = target.collection.find_one({"_id": target.id}) or {}
            if expected_token is not _ANY_DATA_TOKEN and document.get('index_data_token') != expected_token:
                raise lifecycle.IndexRepairSuperseded("This repair is outdated. Indexes were not changed because a newer data load owns the table.")
            if document.get("index_data_ready") is False:
                raise lifecycle.IndexRepairError(
                    "The current data attempt has not committed; resume its data job"
                )
            mode = (
                IndexCreationMode.POST_LOAD.value
                if (
                    document.get("index_data_ready") is True
                    and document.get("index_data_token")
                )
                else IndexCreationMode.ONLINE.value
            )
            return self.reconcile(target, connection, mode=mode)

    def create(self, target, sql, origin, *, adopt=False, require_concurrently=False):
        definition = parse_index(sql)
        if require_concurrently and not definition.concurrently:
            raise ValueError('Rule CREATE requests must specify CONCURRENTLY')
        if (definition.schema, definition.table) != (target.schema, target.table):
            raise ValueError('SQL target does not match selected dataset')
        with self.guard(target) as connection:
            names = {e['index_name'] for e in self.source_policy(target, connection)}
            with connection.cursor() as cursor:
                physical = inspect_index(cursor, target.schema, definition.index_name)
        if definition.index_name in names:
            raise IndexConflict('Source-managed indexes are read-only')
        if physical and (physical.primary or physical.constraint_owned):
            raise IndexConflict(
                f"Constraint-owned index '{definition.index_name}' is read-only"
            )
        entry = save_intent(target.collection, target.id, target.schema, target.table, sql, origin, adopt)
        result = self.reconcile(target)
        return {'index': entry, **result.to_dict()}

    def delete(self, target, name):
        with self.guard(target) as connection:
            names = {e['index_name'] for e in self.source_policy(target, connection)}
        if name in names:
            raise IndexConflict('Source-managed indexes are read-only')
        entry = delete_intent(target.collection, target.id, target.schema, name)
        result = self.reconcile(target)
        return {'index': entry, **result.to_dict()}

    def listing(self, target):
        with self.guard(target) as connection:
            return lifecycle.listing(target.collection, target.id, target.schema, target.table,
                                     connection, self.source_policy(target, connection))

    def register_legacy_population(self, population_id):
        """Convert requests to target ownership once; an existing tombstone wins.

        Existing physical objects still require explicit, verified adoption.
        Legacy arrays never authorize taking over an unstamped catalog object.
        """
        targets = self.population_targets(population_id)
        population = targets[0]
        docs = list(population.document.get('query_indexes') or [])
        for column in population.document.get('indexes') or []:
            name = f'idx_{population.schema}_{population.table}_{column}'.encode()[:63].decode('utf8', errors='ignore')
            docs.append({'sql': f'CREATE INDEX {quote(name)} ON {quote(population.schema)}.{quote(population.table)} ({quote(column)})'})
        for old in docs:
            sql = old.get('sql') or old.get('normalized_sql')
            parsed = parse_index(sql)
            for key in ('schema', 'table', 'index_name'):
                if old.get(key) is not None and old[key] != getattr(parsed, key):
                    raise ValueError('Legacy index metadata does not match its explicit SQL target')
            # A registered request follows its dataset through a rename. The
            # original SQL remains evidence and is rebound only for execution.
            registered = [t for t in targets if any(
                e.get('sql') == sql and e.get('index_name') == parsed.index_name
                for e in (t.collection.find_one({'_id': t.id}) or {}).get('secondary_indexes', []))]
            if registered:
                continue
            target = self.resolve(parsed.schema, parsed.table, targets)
            fresh = target.collection.find_one({'_id': target.id})
            if any(e['index_name'] == parsed.index_name for e in fresh.get('secondary_indexes', [])):
                continue
            save_intent(target.collection, target.id, target.schema, target.table, sql,
                        {'type': 'population', 'id': population_id}, only_if_missing=True)

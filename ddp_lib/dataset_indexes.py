"""Client-neutral target registry and lifecycle integration for Core datasets."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
import json
import re

from ddp_lib import secondary_indexes as lifecycle
from ddp_lib.index_metadata import save_intent, delete_intent, IndexConflict
from ddp_lib.index_sql import parse_index, quote
from ddp_lib.postgres_indexes import dataset_session, inspect_index

KINDS = {'REFERENCE': 'references', 'POPULATION': 'populations', 'CLEANED_TABLE': 'cleaned_tables'}
DEPENDENCIES = {'tables_ids': 'REFERENCE', 'population_ids': 'POPULATION', 'cleaned_table_ids': 'CLEANED_TABLE'}


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
    """Persisted source policy has deterministic identities, separate from intent."""
    if dataset.kind != 'REFERENCE':
        return []
    result = []
    names = set()
    for policy in dataset.document.get('source_indexes') or []:
        if policy.get('primary'):
            continue
        columns = policy.get('columns') or []
        if not columns or any(not isinstance(c, str) or not c for c in columns):
            raise ValueError('Source index policy has unsupported keys; review source metadata')
        unique = bool(policy.get('unique'))
        slug = lambda s: re.sub('_+', '_', re.sub('[^a-z0-9_]+', '_', s.lower())).strip('_')
        name = 'idx_' + '_'.join([slug(dataset.schema), slug(dataset.table)] + [slug(c) for c in columns])
        if unique:
            name += '_uniq'
        identity = json.dumps([dataset.kind, str(dataset.id), columns, unique])
        if len(name.encode()) > 63:
            name = name[:49] + '_' + sha256(identity.encode()).hexdigest()[:12]
        if name in names:
            raise IndexConflict('Source index names collide; review source policy')
        names.add(name)
        sql = ('CREATE ' + ('UNIQUE ' if unique else '') + 'INDEX ' + quote(name)
               + ' ON ' + quote(dataset.schema) + '.' + quote(dataset.table)
               + ' (' + ', '.join(quote(c) for c in columns) + ')')
        excluded = {e['index_name'] for e in dataset.document.get('secondary_indexes', [])}
        definition = parse_index(sql)
        candidates = [p for p in physical if p.index_name not in excluded and not p.primary
                      and not p.constraint_owned and lifecycle.catalog_signature(p, definition) == definition.signature]
        actual = next((p for p in physical if p.index_name == name), None)
        if actual is None and candidates:
            actual = sorted(candidates, key=lambda p: (not p.valid, p.index_name))[0]
        if actual:
            name = actual.index_name
            sql = definition.render(name=name, concurrently=False)
        result.append({'id': 'source:' + sha256(identity.encode()).hexdigest(), 'index_name': name,
            'sql': sql, 'desired_state': 'present', 'adopt': False, 'revision': 1})
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
            raise LookupError(f'Index target dataset not found: {kind} {dataset_id!r}')
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
                # Legacy extraction stored unresolved names as str(None).
                if identifier in (None, 'None'):
                    continue
                target = self.get(kind, identifier)
                targets[(kind, identifier)] = target
        return list(targets.values())

    def population_targets(self, population_id):
        target = self.get('POPULATION', population_id)
        return [target] + [
            dependency
            for dependency in self.inputs(target.document)
            if (dependency.kind, dependency.id) != (target.kind, target.id)
        ]

    def resolve(self, schema, table, allowed):
        matches = [d for d in allowed if (d.schema, d.table) == (schema, table)]
        if len(matches) != 1:
            raise ValueError('SQL target must be exactly one registered dataset dependency')
        return matches[0]

    def factory(self, target):
        return self.connector_factory(target.schema).get_connection

    @contextmanager
    def guard(self, target):
        with dataset_session(self.factory(target), target.collection.full_name, target.id,
                             target.schema, target.table) as connection:
            yield connection

    def source_policy(self, target, connection):
        from ddp_lib.postgres_indexes import list_indexes
        with connection.cursor() as cursor:
            physical = list_indexes(cursor, target.schema, target.table)
        return source_entries(target, physical)

    def reconcile(self, target, connection=None, *, finish=True):
        # Keep the already resolved KAN-11 identity across lease expiry after a
        # reserved SQL commit; ordinary identity backfill may still be buffered.
        from dataclasses import replace
        document = target.collection.find_one({"_id": target.id})
        schema_field = "data_mart_schema" if target.kind == "REFERENCE" else "schema"
        if (not document or document.get("table_name") != target.table
                or document.get(schema_field, target.schema) != target.schema):
            raise IndexConflict("Dataset identity changed; retry")
        target = replace(target, document=document)
        try:
            if connection is None:
                with self.guard(target) as connection:
                    return self.reconcile(target, connection, finish=finish)
            return lifecycle.reconcile(target.collection, target.id, target.schema, target.table,
                self.factory(target), connection, source_entries=self.source_policy(target, connection), finish=finish)
        except Exception as exc:
            target.collection.update_one({"_id": target.id,
                "index_data_token": document.get("index_data_token")}, {"$set": {
                    "index_repair_pending": True, "index_repair_error": str(exc), "indexes_applied": False}})
            raise

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

"""Dataset-owned intent, compare-and-swap writes, and data-attempt checkpoints."""
from copy import deepcopy
from datetime import datetime, timezone
from uuid import uuid4

from ddp_lib.index_sql import parse_index

DATASET_COLLECTIONS = frozenset({'references', 'populations', 'cleaned_tables'})
PROTECTED = frozenset({'_id', 'secondary_indexes', 'secondary_indexes_revision',
    'index_repair_pending', 'index_repair_error', 'index_data_ready',
    'index_data_token', 'index_repair_stage'})
DEFAULTS = {'secondary_indexes': [], 'secondary_indexes_revision': 0,
            'index_repair_pending': False, 'index_repair_error': None}


class IndexConflict(ValueError):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def editable_fields(collection, fields):
    if collection not in DATASET_COLLECTIONS:
        return dict(fields)
    return {k: v for k, v in fields.items() if k.split('.', 1)[0] not in PROTECTED}


def dataset_update(fields):
    return {'$set': fields, '$setOnInsert': deepcopy(DEFAULTS)}


def expected_revision(doc):
    return doc['secondary_indexes_revision'] if 'secondary_indexes_revision' in doc else {'$exists': False}


def mutate(collection, dataset_id, change, *, pending=False):
    for _ in range(8):
        doc = collection.find_one({'_id': dataset_id})
        if not doc:
            raise LookupError('Index target dataset does not exist')
        entries = deepcopy(doc.get('secondary_indexes') or [])
        result = change(entries)
        if result is None:
            return None
        if entries == (doc.get('secondary_indexes') or []):
            return result
        fields = {'secondary_indexes': entries,
                  'secondary_indexes_revision': doc.get('secondary_indexes_revision', 0) + 1}
        if pending:
            fields.update(index_repair_pending=True, indexes_applied=False)
        changed = collection.update_one({'_id': dataset_id,
            'secondary_indexes_revision': expected_revision(doc)}, {'$set': fields})
        if changed.matched_count:
            return result
    raise IndexConflict('Index metadata changed concurrently; retry the operation')


def save_intent(collection, dataset_id, schema, table, raw_sql, origin=None, adopt=False, *, only_if_missing=False):
    definition = parse_index(raw_sql)
    if (definition.schema, definition.table) != (schema, table):
        raise ValueError('Index SQL must target the selected dataset')
    if not isinstance(adopt, bool):
        raise ValueError('adopt must be a boolean')
    def change(entries):
        entry = next((e for e in entries if e['index_name'] == definition.index_name), None)
        if entry and only_if_missing:
            return entry
        if entry and entry['desired_state'] == 'present':
            if parse_index(entry['sql']).signature != definition.signature:
                raise IndexConflict('Index name already has a different desired definition')
            # Explicit adoption of an unowned request is a new revision.
            if not adopt or entry.get('adopt'):
                return entry
        if entry is None:
            entry = {'id': uuid4().hex, 'revision': 0, 'created_at': now()}
            entries.append(entry)
        entry.update(schema=schema, table=table, index_name=definition.index_name,
            sql=raw_sql, desired_state='present', status='pending',
            revision=entry['revision'] + 1, updated_at=now(), origin=origin or {}, adopt=adopt)
        entry.pop('last_error', None)
        entry.pop('signature', None)
        return entry
    return mutate(collection, dataset_id, change, pending=True)


def delete_intent(collection, dataset_id, schema, index_name):
    def change(entries):
        entry = next((e for e in entries if e['index_name'] == index_name), None)
        if entry is None:
            raise IndexConflict('Only managed secondary indexes may be deleted')
        if entry['desired_state'] == 'absent':
            return entry
        entry.update(desired_state='absent', status='pending', revision=entry['revision'] + 1,
                     updated_at=now())
        return entry
    return mutate(collection, dataset_id, change, pending=True)


def record_result(collection, dataset_id, index_id, revision, result):
    def change(entries):
        entry = next((e for e in entries if e['id'] == index_id and e['revision'] == revision), None)
        if entry is None:
            return None
        allowed = {'status', 'signature', 'last_error', 'schema', 'table'}
        entry.update({k: v for k, v in result.items() if k in allowed})
        entry['last_run_at'] = now()
        if result.get('status') in {'created', 'deleted'}:
            entry.pop('last_error', None)
            # Adoption authorizes one checked object, not all future replacements.
            entry['adopt'] = False
        return True
    return bool(mutate(collection, dataset_id, change))


def begin_data(collection, dataset_id, token, *, owner=None):
    query = {'_id': dataset_id}
    if owner:
        query['queue_attempt'] = owner
    if not collection.update_one(query, {'$set': {
        'index_data_token': token, 'index_data_ready': False,
        'index_repair_pending': True, 'indexes_applied': False,
        'index_repair_stage': 'writing',
    }}).matched_count:
        raise IndexConflict('Data attempt no longer owns this dataset')


def data_committed(collection, dataset_id, token):
    if not collection.update_one({'_id': dataset_id, 'index_data_token': token}, {'$set': {
        'index_data_ready': True, 'index_repair_pending': True,
        'index_repair_stage': 'indexing',
    }}).matched_count:
        raise IndexConflict('Data checkpoint belongs to a newer attempt')

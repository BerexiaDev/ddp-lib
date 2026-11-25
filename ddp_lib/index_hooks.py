"""Small hooks around existing data writers; never implements their refresh SQL."""
from contextvars import ContextVar
from functools import wraps
from uuid import uuid4

from ddp_lib.index_metadata import begin_data, data_committed
from ddp_lib.secondary_indexes import IndexRepairError

_attempts = ContextVar('ddp_index_data_attempts', default=None)


def guarded_data(registry_factory, kind, *, document_arg=0, execution_factory=None, close_on_failure=(), restore_after=False):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            registry = registry_factory()
            document = args[document_arg]
            target = None
            entered = False
            try:
                target = registry.get(kind, document.id)
                with registry.guard(target):
                    entered = True
                    execution = execution_factory() if execution_factory else None
                    if execution:
                        execution.job.assert_owned()
                    token = execution.job.attempt_id if execution else uuid4().hex
                    begin_data(target.collection, target.id, token,
                               owner=execution.job.attempt_id if execution else None)
                    previous = _attempts.get() or {}
                    context = _attempts.set(dict(previous, **{str(target.id): (token, target)}))
                    try:
                        result = function(*args, **kwargs)
                        if restore_after:
                            committed(registry, kind, document)
                            registry.reconcile(target)
                        return result
                    finally:
                        _attempts.reset(context)
            except Exception as exc:
                if target is not None:
                    target.collection.update_one({'_id': target.id, 'index_data_token': locals().get('token')}, {'$set': {
                        'index_repair_pending': True, 'index_repair_error': str(exc)}})
                if not entered:
                    for position in close_on_failure:
                        if len(args) > position and args[position] is not None:
                            args[position].close()
                raise
        return wrapped
    return decorate


def committed(registry, kind, document):
    attempt = (_attempts.get() or {}).get(str(document.id))
    if attempt is None:
        raise IndexRepairError('Missing data-writing attempt checkpoint')
    token, target = attempt
    data_committed(target.collection, target.id, token)
    return target

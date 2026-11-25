"""Active queue identities shared by API producers, Sync and rollout recovery."""

from uuid import uuid4

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

REPAIR_ACTION = "RECONCILE_INDEXES"


def active_job_key(data):
    # Retain the deployed data-job key. Only repair receives a separate lane.
    key = f"{data.get('schema_name')}:{data['target_id']}"
    return key + ":" + REPAIR_ACTION if data.get("action") == REPAIR_ACTION else key


def enqueue(collection, data):
    """Atomically deduplicate data jobs and repair jobs independently.

    All producers must be upgraded together. Move an old repair key with a CAS
    before upsert, so an existing repair cannot absorb a new data refresh.
    Conflicting legacy repair documents fail closed for operator review.
    """
    collection.create_index(
        "active_key",
        unique=True,
        partialFilterExpression={"active_key": {"$exists": True}},
    )
    data = dict(data)
    data.setdefault("_id", uuid4().hex)
    base = active_job_key(dict(data, action=None))
    collection.update_one(
        {"active_key": base, "action": REPAIR_ACTION},
        {"$set": {"active_key": base + ":" + REPAIR_ACTION}},
    )
    data["active_key"] = active_job_key(data)
    try:
        return collection.find_one_and_update(
            {"active_key": data["active_key"]},
            {"$setOnInsert": data},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
    except DuplicateKeyError:
        existing = collection.find_one({"active_key": data["active_key"]})
        if existing is None:
            raise
        return existing

from cmr_etl_lib.audit_logger.models.audit_trail import AuditTrail
from cmr_etl_lib.decorators import catch_exceptions
from cmr_etl_lib.paginator import Paginator
from cmr_etl_lib.filters import build_filters


@catch_exceptions
def get_audit_logs_paginated(args, data):
    query = build_filters(data.get("filters", []))

    if "action" not in query:
        query["action"] = {"$ne": "RETRIEVE"}

    page = args.get("page")
    per_page = args.get("size")
    sort_by = args.get("sort_key", "id")
    sort_order = args.get("sort_order", -1)

    collection = AuditTrail().db()
    skip = max((page - 1) * per_page, 0)
    
    
    if "action" not in query:
        query["action"] = {"$ne": "RETRIEVE"}
    
    total = AuditTrail.count(query)
    if skip >= total:
        skip = max(total - per_page, 0)
            
    total_items = collection.aggregate(
        [
            {"$match": query},
            {"$sort": {sort_by: sort_order}},
            {"$skip": skip},
            {"$limit": per_page},
        ]
    )

    data = [AuditTrail(**entity) for entity in total_items]
    return Paginator(data, page, per_page, total)
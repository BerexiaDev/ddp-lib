from cmr_etl_lib.document import Document


class AuditTrail(Document):
    __TABLE__ = "audit_trails"

    _id = None
    collection = None
    action = None
    endpoint = None
    user = None
    old_value = None
    new_value = None
    created_on = None

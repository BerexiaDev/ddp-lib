from datetime import datetime
from flask import Blueprint, request, g

from cmr_etl_lib.audit_logger.models.audit_trail import AuditTrail
from cmr_etl_lib.audit_logger.utils import get_json_body, get_only_changed_values_and_id, get_action, get_primary_key_value
from cmr_etl_lib.audit_logger.utils import IGNORE_PATHS
from cmr_etl_lib.auth.user import User

SUCCESS_STATUS_CODES = [200, 201, 204]
DEFAULT_LOG_METHODS = ["POST", "PUT", "DELETE", "PATCH"]
PRIMARY_KEY_MAPPING = {
    "users": "email",
    "connectors": "name",
    "refrences":"name",
    "dictionaries": "name",
    "processes": "name",
    "rules": "name",
    "issues": "name",
    "redemiation_actions": "description",
    "populations": "name",
    "domains": "name"
}
AUDIT_COLLECTION_NAME = "audit_trails"



class AuditBlueprint(Blueprint):
    """
        AuditBlueprint is a blueprint that logs changes to a collection in a MongoDB database.
    """
    def __init__(self, *args, **kwargs):
        self.log_methods = kwargs.pop("log_methods", DEFAULT_LOG_METHODS)
        self.audit_collection = None

        super(AuditBlueprint, self).__init__(*args, **kwargs)
        self.after_request(self.after_data_request)

    def _is_loggable(self, response) -> bool:
        return request.method in self.log_methods and response.status_code in SUCCESS_STATUS_CODES

    def after_data_request(self, response):
        table_name = g.get("table_name")
        endpoint = request.path
        
        if not table_name or table_name == AUDIT_COLLECTION_NAME or endpoint in IGNORE_PATHS or "swagger" in endpoint or request.method == "OPTIONS" or "search" in endpoint:
            return response
        

        primary_key = PRIMARY_KEY_MAPPING.get(table_name, "name")
        primary_key_splits = primary_key.split(".")

        if self._is_loggable(response):
            old_data = g.get("old_data", None)

            if g.get("new_data"):
                new_data = g.new_data
            else:
                new_data = get_json_body(request)

            if request.method == 'DELETE':
                new_data = new_data or None
                if old_data:
                    if isinstance(old_data, list):
                        old_data = [
                            {
                                "_id": d.get("_id"),
                                "name": get_primary_key_value(primary_key_splits, d)
                            } for d in old_data
                        ]
                    else:
                        _id = old_data.get("_id")
                        primary_value = get_primary_key_value(primary_key_splits, old_data)
                        old_data = {
                            "_id": _id,
                            "name": primary_value
                        }

            elif request.method == 'GET':
                new_data = old_data = None
            else:
                if g.get("new_data") is None:
                    new_data, old_data = get_only_changed_values_and_id(old_data or {}, new_data) if old_data else (new_data, old_data)

                if response.status_code == 201:
                    if isinstance(new_data, list):
                        final_value = [get_primary_key_value(primary_key_splits, d) for d in new_data]
                        new_data = {
                            "name": ",".join(final_value) if final_value else ""
                        }
                    else:
                        primary_value = get_primary_key_value(primary_key_splits, new_data)
                        new_data = {
                            "name": primary_value
                        }


            action = get_action(request.method, response.status_code)
            auth_token = request.headers.get('Authorization').split(" ")[1]
            decode_resp = User.decode_auth_token(auth_token)
            user = User().load({'_id':decode_resp.get("token")})
            self.create_log(action, table_name, endpoint, new_value=new_data, old_value=old_data, user_info=user.to_dict())

        return response

    def create_log(self, action: str, table_name: str, endpoint: str, new_value=None, old_value=None, user_info=None):
        user_info = user_info if user_info else {"email": "system@email.com", "fullname": "System User"}

        audit_log = {
            "collection": table_name,
            "action": action,
            "endpoint": endpoint,
            "user": {
              "id": user_info.get("id"),
              "email": user_info.get("email"),
              "full_name": user_info.get("full_name")
            },
            "old_value": old_value,
            "new_value": new_value,
            "created_on": datetime.utcnow()
        }
        action = AuditTrail(**audit_log)
        action.save()

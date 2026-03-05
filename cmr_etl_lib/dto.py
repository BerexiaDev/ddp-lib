import datetime

from flask_restx import Namespace, fields


class NullableString(fields.String):
    __schema_type__ = ["string", "null"]
    __schema_example__ = "nullable string"


class NullableInteger(fields.Integer):
    __schema_type__ = ["integer", "null"]
    __schema_example__ = "nullable integer"


class NullableFloat(fields.Float):
    __schema_type__ = ["number", "null"]
    __schema_example__ = "nullable float"


class NullableBoolean(fields.Boolean):
    __schema_type__ = ["boolean", "null"]
    __schema_example__ = "nullable boolean"


class DynamicField(fields.Raw):
    def format(self, value):
        return self.serialize_field(value)

    @staticmethod
    def serialize_field(value):
        if isinstance(value, datetime.datetime):
            return value.isoformat()
        if isinstance(value, datetime.date):
            return value.isoformat()
        if isinstance(value, dict):
            return {k: DynamicField.serialize_field(v) for k, v in value.items()}
        if isinstance(value, list):
            return [DynamicField.serialize_field(v) for v in value]
        return value


class AuditDto:
    api = Namespace("AuditTrail")

    user_info = api.model(
        "User",
        {
            "id": fields.String(),
            "full_name": fields.String(required=True),
            "email": fields.String(required=True),
        },
    )

    audit_info = api.model(
        "AuditTrail Info",
        {
            "id": fields.String(required=True),
            "collection": NullableString(),
            "action": fields.String(required=True),
            "user": fields.Nested(user_info),
            "old_value": DynamicField(),
            "new_value": DynamicField(),
            "created_on": fields.DateTime(),
        },
    )

    audit_pagination = api.model(
        "AuditTrail page",
        {
            "page": fields.Integer,
            "size": fields.Integer,
            "total": fields.Integer,
            "content": fields.List(fields.Nested(audit_info), skip_none=True),
        },
    )

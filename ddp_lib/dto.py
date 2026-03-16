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


def create_response_dto(api, model_name, model=None, response_type="base", is_list=False):
    """
    Create a standardized Flask-RESTx response DTO.

    response_type:
        - "base"       -> status + message
        - "data"       -> status + message + data
        - "pagination" -> status + message + content + total + page + size

    :param api: Flask-RESTx API/Namespace instance
    :param model_name: Name of the response model
    :param model: DTO model to include in data/content
    :param response_type: "base", "data", or "pagination"
    :param is_list: If True and response_type="data", wraps data as a list
    :return: Flask-RESTx model
    """

    base_model = api.model(f"{model_name}Base", {
        "status": fields.String(
            description="Response status",
            skip_none=True,
            example="success"
        ),
        "message": fields.String(
            description="Response message",
            skip_none=True,
            example="Request successful"
        ),
    })

    if response_type == "base":
        return base_model

    if response_type == "data":
        if model:
            data_field = (
                fields.List(fields.Nested(model), description="Response data")
                if is_list
                else fields.Nested(model, description="Response data")
            )
        else:
            data_field = fields.Raw(description="Response data")

        return api.inherit(model_name, base_model, {
            "data": data_field
        })

    if response_type == "pagination":
        content_field = (
            fields.List(fields.Nested(model), description="Response data")
            if model
            else fields.Raw(description="Response data")
        )

        return api.inherit(model_name, base_model, {
            "content": content_field,
            "total": fields.Integer(description="Total number of records", required=True),
            "page": fields.Integer(description="Current page number", required=True),
            "size": fields.Integer(description="Number of records per page", required=True),
        })

    raise ValueError("response_type must be one of: 'base', 'data', 'pagination'")



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

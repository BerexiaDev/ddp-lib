import re
from datetime import datetime

def get_ids_by_name(collection, name_field, id_field, name_value):
    """
    Fetches the ID corresponding to a given name from a MongoDB collection.
    """
    results = collection.find(
        {name_field: {"$regex": f"{name_value.strip()}", "$options": "i"}},
        {id_field: 1},
    )

    if results:
        return [r[id_field] for r in results]


def build_filters(filters):
    """
    Converts the filters object into a MongoDB query.

    :param filters: Array containing filter information.
    :return: MongoDB query as a dictionary.
    """
    mongo_query = {}

    for filter_item in filters:
        # Extract filter components
        table_name, field_info = filter_item.get("field", [None, {}])
        field_code = field_info.get("code", None)
        field_type = field_info.get("type", None)
        operator = filter_item.get("operator", None)
        value = filter_item.get("value", None)

        if not table_name:
            raise ValueError("No table name")
        if not field_code:
            raise ValueError("No columns name")
        if not field_type:
            raise ValueError("No field type")
        if not operator:
            raise ValueError("No operator")
        if value != 0 and not value:  # Allow 0 as a valid value
            raise ValueError("No value provided.")

        # Handle audit_trails with user fullname or email search
        if table_name == "audit-trails" and field_code in ["full_name", "email"]:
            mongo_query[f"user.{field_code}"] = _build_query_for_operator(operator, value)
            continue

        if table_name == "users" and field_code == "has_backup":
            if table_name == "users" and field_code == "has_backup":
                mongo_query["backup_id"] = {"$ne": None} if value else None
                continue

        # Handle date-specific operators
        if operator in ["BEFORE", "AFTER"]:
            if not isinstance(value, (str)):
                raise ValueError(
                    f"Value for '{operator}' operator must be a date string."
                )
                
            value_datetime = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")

            if operator == "BEFORE":
                mongo_query[field_code] = {"$lt": value_datetime}
            elif operator == "AFTER":
                mongo_query[field_code] = {"$gt": value_datetime}

        elif operator == "EQUALS":
            mongo_query[field_code] = value
        elif operator == "NOT EQUALS":
            mongo_query[field_code] = {"$ne": value}
        elif operator == "CONTAINS":
            if not isinstance(value, str):
                raise ValueError("Value for 'CONTAINS' operator must be a string.")
            mongo_query[field_code] = {"$regex": value, "$options": "i"}
        elif operator == "IN":
            regex_query = [re.compile(v, re.IGNORECASE) for v in value]
            mongo_query[field_code] = {"$in": regex_query}
        elif operator == "GREATER THAN":
            mongo_query[field_code] = {"$gt": value}
        elif operator == "LESS THAN":
            mongo_query[field_code] = {"$lt": value}
        else:
            raise ValueError(f"Unsupported operator: {operator}")

    return mongo_query


def _build_query_for_operator(operator, value):
    """
    Helper function to build query based on operator for user fields in audit_trails
    """
    if operator == "EQUALS":
        return value
    elif operator == "NOT EQUALS":
        return {"$ne": value}
    elif operator == "CONTAINS":
        if not isinstance(value, str):
            raise ValueError("Value for 'CONTAINS' operator must be a string.")
        return {"$regex": value, "$options": "i"}
    elif operator == "IN":
        regex_query = [re.compile(v, re.IGNORECASE) for v in value]
        return {"$in": regex_query}
    else:
        raise ValueError(f"Unsupported operator for user fields: {operator}")
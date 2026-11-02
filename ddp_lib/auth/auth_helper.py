import jwt
from flask import g

from ddp_lib.auth.tokens import bearer_token
from ddp_lib.auth.user import User


class AuthHelper:
    @staticmethod
    def get_logged_in_user(request):
        """Validate the access token and load the active user for this request."""
        g.pop("user", None)
        try:
            auth_token = bearer_token(request.headers.get('Authorization'))
        except jwt.InvalidTokenError:
            return {"status": "fail", "message": "Provide a valid bearer token."}, 401

        result = User.decode_auth_token(auth_token)
        if result.get("status") != "success":
            return result, 401

        user = User().load({'_id': result['token']})
        if user.id is None:
            return {"status": "fail", "message": "User not found."}, 401
        if not user.is_active or (
            getattr(user, "auth_provider", None) == "keycloak" and not user.role
        ):
            return {"status": "fail", "message": "Invalid user session."}, 401

        g.user = user
        return {
            'status': 'success',
            'data': {
                'id': user.id,
                'email': user.email,
                'full_name': user.full_name,
                'is_active': user.is_active,
                'references_ids': user.references_ids,
                'populations_ids': user.populations_ids,
                'domains_ids': user.domains_ids,
                'sub_domains_ids': user.sub_domains_ids,
                'created_on': str(user.created_on),
                'role': user.role,
            },
        }, 200

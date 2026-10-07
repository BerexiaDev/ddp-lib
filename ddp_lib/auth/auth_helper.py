from ddp_lib.auth.user import User
from flask import g
class AuthHelper:
    
    @staticmethod
    def get_logged_in_user(request, *, strict=False):
        """Return the existing user/status tuple.

        strict=True validates bearer headers, decoder results and user existence.
        The default preserves the legacy request/error contract.
        """
        authorization = request.headers.get('Authorization')
        if strict:
            g.pop("user", None)
            parts = (authorization or "").split()
            if len(parts) != 2 or parts[0].lower() != "bearer":
                return {"status": "fail", "message": "Provide a valid bearer token."}, 401
            auth_token = parts[1]
        else:
            auth_token = authorization.split(" ")[1]
        if auth_token:
            try:
                resp = User.decode_auth_token(auth_token)
            except KeyError as error:
                if not strict or error.args != ("sub",):
                    raise
                return {"status": "fail", "message": "Invalid token."}, 401
            if strict and (
                not isinstance(resp, dict)
                or resp.get("status") != "success"
                or resp.get("token") in (None, "")
                or isinstance(resp.get("token"), (dict, list))
            ):
                message = resp.get("message", "Invalid token.") if isinstance(resp, dict) else str(resp)
                return {"status": "fail", "message": message}, 401
            if not isinstance(resp, str):
                user = User().load({'_id':resp['token']})
                if strict and user.id is None:
                    return {"status": "fail", "message": "User not found."}, 401
                g.user = user
                response_object = {
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
                        'role': user.role
                    }
                }
                return response_object, 200
            response_object = {
                'status': 'fail',
                'message': resp
            }
            return response_object, 401
        else:
            response_object = {
                'status': 'fail',
                'message': 'Provide a valid auth token.'
            }
            return response_object, 401

from cmr_etl_lib.auth.user import User

class AuthHelper:
    
    @staticmethod
    def get_logged_in_user(request):
        authorization = request.headers.get('Authorization')
        auth_token = authorization.split(" ")[1]
        if auth_token:
            resp = User.decode_auth_token(auth_token)
            if not isinstance(resp, str):
                user = User().load({'_id':resp['token']})
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
    
    @staticmethod
    def get_logged_in_user_from_token(token):
        resp = User.decode_auth_token(token)
        if not isinstance(resp, str):
            user = User().load({'_id':resp['token']})
            response_object = {
                'status': 'success',
                'data': {
                    'id': user.id,
                    'email': user.email,
                    'full_name': user.full_name,
                    'is_active': user.is_active,
                    'references': user.references,
                    'process': user.process,
                    'created_on': str(user.created_on),
                    'role': user.role
                }
            }
            return response_object
import datetime
import inject
import jwt
import os
from cmr_etl_lib.auth.black_list_token import BlacklistToken
from cmr_etl_lib.document import Document
from flask_bcrypt import Bcrypt


class User(Document):
    __TABLE__ = "users"

    email = None
    password_hash = None
    full_name = None
    created_on = None
    modified_on = None
    admin = None
    role = None
    is_active = None
    is_new_user = None
    domains_ids = []
    sub_domains_ids = []
    references_ids = []
    populations_ids = []
    

    @property
    def password(self):
        raise AttributeError('password: write-only field')

    @password.setter
    def password(self, password):
        flask_bcrypt = inject.instance(Bcrypt)
        self.password_hash = flask_bcrypt.generate_password_hash(password).decode('utf-8')

    def check_password(self, password):
        flask_bcrypt = inject.instance(Bcrypt)
        return flask_bcrypt.check_password_hash(self.password_hash, password)

    @staticmethod
    def encode_auth_token(user_id, days= 1, seconds=5, minutes= 0):
        """
        Generates the Auth Token
        :return: string
        """
        try:
            key = os.getenv("SECRET_KEY")
            payload = {
                'exp': datetime.datetime.utcnow() + datetime.timedelta(days= days, seconds=seconds, minutes=minutes),
                'iat': datetime.datetime.utcnow(),
                'sub': user_id
            }
            return jwt.encode(
                payload,
                key,
                algorithm='HS256'
            )
        except Exception as e:
            return e
        
    @staticmethod
    def encode_refresh_token(user_id, days=30):
        """
        Generates the Refresh Token
        :return: string
        """
        try:
            key = os.getenv("SECRET_KEY")
            payload = {
                'exp': datetime.datetime.utcnow() + datetime.timedelta(days=days),
                'iat': datetime.datetime.utcnow(),
                'sub': user_id
            }
            return jwt.encode(
                payload,
                key,
                algorithm='HS256'
            )
        except Exception as e:
            return str(e)

    @staticmethod
    def decode_auth_token(auth_token):
        """
        Decodes the auth token
        :param auth_token:
        :return: integer|string
        """
        try:
            key = os.getenv("SECRET_KEY")
            payload = jwt.decode(auth_token, key)
            is_blacklisted_token = BlacklistToken.check_blacklist(auth_token)
            if is_blacklisted_token:
                return {
                     "status": "fail",
                     "message": "Token blacklisted. Please log in again.",
                    } 
            else:
                return {"status": "success", "token": payload["sub"]}
        except jwt.ExpiredSignatureError:
            return {
                "status": "fail",
                "message": "Signature expired. Please log in again.",
            }
        except jwt.InvalidTokenError:
            return {"status": "fail", "message": "Invalid token. Please log in again."}
    def __repr__(self):
        return "<User '{} {}'>".format(self.first_name,self.last_name)



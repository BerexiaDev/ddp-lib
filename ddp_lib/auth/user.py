import datetime
import inject
import jwt
from ddp_lib.auth.tokens import decode_token, encode_token
from ddp_lib.auth.black_list_token import BlacklistToken
from ddp_lib.document import Document
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
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(
            days=days, seconds=seconds, minutes=minutes)
        return encode_token(user_id, "access", expires_at.timestamp())
        
    @staticmethod
    def encode_refresh_token(user_id, days=30):
        """
        Generates the Refresh Token
        :return: string
        """
        expires_at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=days)
        return encode_token(user_id, "refresh", expires_at.timestamp())

    @staticmethod
    def decode_auth_token(auth_token):
        """
        Decodes the auth token
        :param auth_token:
        :return: integer|string
        """
        try:
            payload = decode_token(auth_token, "access")
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

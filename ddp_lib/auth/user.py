import datetime
import inject
import jwt
import os
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
    def _secret_key():
        key = os.getenv("SECRET_KEY")
        if not key:
            raise ValueError("SECRET_KEY must be configured for token operations")
        return key

    @staticmethod
    def _encode_token(user_id, lifetime):
        if not isinstance(user_id, str) or not user_id:
            raise ValueError("user_id must be a non-empty string")
        now = datetime.datetime.now(datetime.timezone.utc)
        return jwt.encode(
            {'exp': now + lifetime, 'iat': now, 'sub': user_id},
            User._secret_key(),
            algorithm='HS256',
        )

    @staticmethod
    def encode_auth_token(user_id, days= 1, seconds=5, minutes= 0):
        """Return an HS256 JWT string; invalid configuration/input raises."""
        return User._encode_token(
            user_id, datetime.timedelta(days=days, seconds=seconds, minutes=minutes)
        )
        
    @staticmethod
    def encode_refresh_token(user_id, days=30):
        """Return an HS256 JWT string; invalid configuration/input raises."""
        return User._encode_token(user_id, datetime.timedelta(days=days))

    @staticmethod
    def decode_auth_token(auth_token):
        """
        Decodes the auth token
        :param auth_token:
        :return: dict with status and token (string subject) or failure message
        """
        try:
            key = User._secret_key()
            if isinstance(auth_token, bytes):
                try:
                    auth_token = auth_token.decode('utf-8')
                except UnicodeDecodeError:
                    raise jwt.InvalidTokenError("Invalid token encoding") from None
            payload = jwt.decode(
                auth_token, key, algorithms=['HS256'],
                options={'require': ['exp', 'iat', 'sub']},
            )
            # Keep the same subject contract on every supported PyJWT 2.x version.
            if not isinstance(payload['sub'], str) or not payload['sub']:
                raise jwt.InvalidTokenError("Subject must be a non-empty string")
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


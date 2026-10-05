import datetime
import os
import unittest
from unittest.mock import patch

import jwt

from ddp_lib.auth.black_list_token import BlacklistToken
from ddp_lib.auth.user import User


# Test signing key (long enough for HS256/HS384/HS512)
SECRET = "smoke-test-secret-with-at-least-64-bytes-for-all-tested-hmac-algorithms"


class TokenContractTests(unittest.TestCase):
    def setUp(self):
        # Set SECRET_KEY for each test, restored afterwards
        env = patch.dict(os.environ, {"SECRET_KEY": SECRET})
        env.start()
        self.addCleanup(env.stop)
        # Mock the blacklist lookup so no database is needed (returns "not blacklisted")
        blacklist = patch.object(BlacklistToken, "check_blacklist", return_value=False)
        self.blacklist = blacklist.start()
        self.addCleanup(blacklist.stop)

    # Access and refresh tokens encode, decode, and have the right lifetime
    def test_access_and_refresh_round_trip(self):
        for encode, lifetime in [(User.encode_auth_token, 86405),
                                 (User.encode_refresh_token, 30 * 86400)]:
            with self.subTest(encode=encode.__name__):
                token = encode("core-user")
                self.assertIsInstance(token, str)
                # Decode directly with PyJWT to check the raw claims
                payload = jwt.decode(token, SECRET, algorithms=["HS256"])
                self.assertEqual(payload["exp"] - payload["iat"], lifetime)
                # Our own decoder returns the original user id
                self.assertEqual(User.decode_auth_token(token),
                                 {"status": "success", "token": "core-user"})

    # A bytes token is looked up in the blacklist as a str
    def test_bytes_use_same_blacklist_key_as_strings(self):
        token = User.encode_auth_token("core-user")
        self.blacklist.return_value = True  # pretend the token is blacklisted
        for value in [token, token.encode("utf-8")]:
            result = User.decode_auth_token(value)
            self.assertEqual(result["status"], "fail")
            self.assertIn("blacklisted", result["message"])
            self.blacklist.assert_called_with(token)

    # An expired token is rejected before any blacklist lookup
    def test_expired_token(self):
        token = User.encode_auth_token("core-user", days=-1, seconds=0)
        self.assertIn("expired", User.decode_auth_token(token)["message"])
        self.blacklist.assert_not_called()

    # Malformed or forged tokens fail without touching the blacklist
    def test_invalid_tokens_fail_before_blacklist_lookup(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        claims = {"sub": "core-user", "iat": now, "exp": now + datetime.timedelta(minutes=1)}
        # Garbage, invalid bytes, wrong signing key, wrong algorithm
        invalid = ["broken", b"\xff", jwt.encode(claims, SECRET + "wrong", algorithm="HS256"),
                   jwt.encode(claims, SECRET, algorithm="HS384")]
        # Invalid subjects (not a non-empty string)
        for subject in [42, None, ""]:
            invalid.append(jwt.encode({**claims, "sub": subject}, SECRET, algorithm="HS256"))
        # Missing required claims
        for missing in ["sub", "exp", "iat"]:
            invalid.append(jwt.encode({k: v for k, v in claims.items() if k != missing},
                                      SECRET, algorithm="HS256"))
        for token in invalid:
            with self.subTest(token=token):
                self.assertEqual(User.decode_auth_token(token)["status"], "fail")
        self.blacklist.assert_not_called()

    # Encoding refuses a user id that is not a non-empty string
    def test_invalid_subject_raises_on_encode(self):
        for encode in [User.encode_auth_token, User.encode_refresh_token]:
            for subject in [42, None, ""]:
                with self.subTest(encode=encode.__name__, subject=subject):
                    with self.assertRaisesRegex(ValueError, "user_id"):
                        encode(subject)

    # Without SECRET_KEY, every token operation raises a clear error
    def test_missing_secret_is_a_configuration_error(self):
        os.environ.pop("SECRET_KEY")
        for operation in [User.encode_auth_token, User.encode_refresh_token,
                          User.decode_auth_token]:
            with self.subTest(operation=operation.__name__):
                with self.assertRaisesRegex(ValueError, "SECRET_KEY"):
                    operation("core-user")


class MongoRuntimeCompatibilityTests(unittest.TestCase):
    # Check the installed pymongo/bson still offers the old APIs we rely on
    def test_legacy_mongo_driver_contract(self):
        from bson import BSON
        from pymongo.collection import Collection

        # These APIs are still used by ddp-lib and Core's document helpers.
        for name in ("save", "remove", "insert"):
            with self.subTest(method=name):
                self.assertTrue(callable(getattr(Collection, name, None)))
        # A document survives a BSON encode/decode round trip
        document = {"_id": "core-user", "active": True}
        self.assertEqual(BSON(BSON.encode(document)).decode(), document)


if __name__ == "__main__":
    unittest.main()
"""Contracts used by existing consumers, including legacy auth defaults."""
import os
import unittest
from unittest.mock import Mock, patch

import jwt
from flask import Flask, g, request

from ddp_lib.auth.auth_helper import AuthHelper
from ddp_lib.auth.black_list_token import BlacklistToken
from ddp_lib.auth.decorator import token_required
from ddp_lib.auth.user import User
from ddp_lib.audit_logger.utils import get_only_changed_values
from ddp_lib.enums import ExecutionStatus, ProjectStatus, QueuedJobAction, SyncStatus
from ddp_lib.utils import create_reference_lookups, serialize_if_needed


SECRET = "contract-test-key-that-is-not-a-deployed-secret"
USER = dict(_id="user-123", email="reader@example.test", full_name="Reader",
            is_active=True, created_on="2026-01-01", role="DATA_OWNER",
            references_ids=["ref-1"], populations_ids=[], domains_ids=["domain-1"],
            sub_domains_ids=[])


class AuthFixture(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.enterContext(patch.dict(os.environ, {"SECRET_KEY": SECRET}))
        self.blacklist = Mock()
        self.blacklist.find_one.return_value = None
        self.enterContext(patch.object(BlacklistToken, "db", return_value=self.blacklist))
        self.users = Mock()
        self.users.find_one.return_value = USER.copy()
        self.enterContext(patch.object(User, "db", return_value=self.users))

    # unittest.TestCase.enterContext was introduced in Python 3.11.
    def enterContext(self, manager):
        value = manager.__enter__()
        self.addCleanup(manager.__exit__, None, None, None)
        return value


class AuthContracts(AuthFixture):
    def test_token_strings_claims_lifetimes_and_decode_shape(self):
        for encode, lifetime in [(User.encode_auth_token, 86405),
                                 (User.encode_refresh_token, 2592000)]:
            with self.subTest(encoder=encode.__name__):
                token = encode("user-123")
                self.assertIsInstance(token, str)
                claims = jwt.decode(token, SECRET, algorithms=["HS256"])
                self.assertEqual(claims["sub"], "user-123")
                self.assertEqual(claims["exp"] - claims["iat"], lifetime)
                self.assertEqual(User.decode_auth_token(token),
                                 {"status": "success", "token": "user-123"})
                self.assertEqual(User.decode_auth_token(token.encode()),
                                 {"status": "success", "token": "user-123"})

    def test_custom_positional_token_lifetime_and_subject_are_preserved(self):
        token = User.encode_auth_token(42, 0, 20, 3)
        claims = jwt.decode(token, SECRET, algorithms=["HS256"])
        self.assertEqual(claims["sub"], 42)
        self.assertEqual(claims["exp"] - claims["iat"], 200)

    def test_token_errors_keep_existing_return_contract(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertIsInstance(User.encode_auth_token("user-123"), Exception)
            self.assertIsInstance(User.encode_refresh_token("user-123"), str)
        self.assertEqual(User.decode_auth_token("broken")["status"], "fail")
        expired = User.encode_auth_token("user-123", days=-1)
        self.assertIn("expired", User.decode_auth_token(expired)["message"])

    def test_blacklisted_and_wrong_algorithm_tokens_fail(self):
        token = User.encode_auth_token("user-123")
        self.blacklist.find_one.return_value = {"_id": "blocked", "token": token}
        self.assertIn("blacklisted", User.decode_auth_token(token)["message"])
        self.blacklist.find_one.return_value = None
        wrong_algorithm = jwt.encode({"sub": "user-123"}, SECRET, algorithm="HS384")
        self.assertEqual(User.decode_auth_token(wrong_algorithm)["status"], "fail")

    def test_request_helper_fields_and_context(self):
        token = User.encode_auth_token("user-123")
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer " + token}):
            result, status = AuthHelper.get_logged_in_user(request)
            self.assertEqual(status, 200)
            self.assertEqual(result, {"status": "success", "data": {
                "id": "user-123", "email": "reader@example.test", "full_name": "Reader",
                "is_active": True, "created_on": "2026-01-01", "role": "DATA_OWNER",
                "references_ids": ["ref-1"], "populations_ids": [],
                "domains_ids": ["domain-1"], "sub_domains_ids": []}})
            self.assertEqual(g.user.id, "user-123")

    def test_default_role_semantics_and_result_shape(self):
        token = User.encode_auth_token("user-123")
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer " + token}):
            for roles in (None, [], ["DATA_OWNER"]):
                self.assertEqual(token_required(roles)(lambda: ({"ok": True}, 202))(),
                                 ({"ok": True}, 202))
            self.assertEqual(token_required(["ADMIN"])(lambda: None)(),
                             ({"message": "Permission denied"}, 403))

    def test_default_missing_and_invalid_token_responses(self):
        with self.app.test_request_context("/protected"):
            self.assertEqual(token_required()(lambda: None)(), ({"message": "Token is missing"}, 401))
            with self.assertRaises(AttributeError):
                AuthHelper.get_logged_in_user(request)
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer broken"}):
            self.assertEqual(token_required()(lambda: None)(),
                             ({"message": "Authentication error: 'token'"}, 401))

    def test_existing_auth_route_and_options_exclusions(self):
        for path, method in [("/health", "GET"), ("/auth/login", "POST"), ("/protected", "OPTIONS")]:
            with self.app.test_request_context(path, method=method):
                self.assertEqual(token_required(["ADMIN"])(lambda: "allowed")(), "allowed")


class HelperContracts(unittest.TestCase):
    def test_mixed_list_diff_and_unordered_scalar_lists(self):
        self.assertEqual(get_only_changed_values({"a": [1, "a"]}, {"a": [1, "b"]}),
                         ({"a": [1, "b"]}, {"a": [1, "a"]}))
        self.assertEqual(get_only_changed_values({"a": [2, 1]}, {"a": [1, 2]}), ({}, {}))

    def test_serialization_keeps_scalar_types(self):
        self.assertEqual(serialize_if_needed({"a": [1]}), '{"a": [1]}')
        for value in (None, 0, False, "text"):
            self.assertIs(serialize_if_needed(value), value)

    def test_lookup_and_enum_wire_values(self):
        self.assertEqual(create_reference_lookups({"author": {"collection": "users"}}), [
            {"$lookup": {"from": "users", "localField": "author", "foreignField": "_id", "as": "author"}},
            {"$unwind": {"path": "$author", "preserveNullAndEmptyArrays": True}}])
        self.assertEqual([ExecutionStatus.CANCELLING.value, ExecutionStatus.CANCELLED.value,
                          SyncStatus.SKIPPED.value, SyncStatus.SCRIPT_ERROR.value,
                          QueuedJobAction.INCREMENTAL.value, ProjectStatus.PAUSED.value],
                         ["CANCELLING", "CANCELLED", "SKIPPED", "SCRIPT_ERROR", "INCREMENTAL", "PAUSED"])


if __name__ == "__main__":
    unittest.main()

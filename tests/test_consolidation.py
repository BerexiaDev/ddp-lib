import unittest
import os
import jwt
from unittest.mock import Mock, patch

from flask import g, request

from ddp_lib.auth.auth_helper import AuthHelper
from ddp_lib.auth.decorator import token_required
from ddp_lib.auth.user import User
from ddp_lib.audit_logger import AuditBlueprint
from ddp_lib.audit_logger.models.audit_trail import AuditTrail
from ddp_lib.enums import AppModuleEnum, UserActionEnum
from ddp_lib.permission_utils import get_allowed_roles_for
import test_contracts as contracts


class StrictAuthTests(contracts.AuthFixture):
    def test_strict_helper_rejects_missing_or_malformed_bearer_headers(self):
        for header in [None, "", "Bearer", "Bearer ", "Basic token", "Bearer token extra"]:
            with self.subTest(header=header):
                headers = {} if header is None else {"Authorization": header}
                with self.app.test_request_context("/protected", headers=headers):
                    result, status = AuthHelper.get_logged_in_user(request, strict=True)
                    self.assertEqual(status, 401)
                    self.assertEqual(result["status"], "fail")
                    self.assertNotIn("user", g)
        self.users.find_one.assert_not_called()

    def test_strict_helper_handles_decoder_failure_without_user_lookup(self):
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer broken"}):
            result, status = AuthHelper.get_logged_in_user(request, strict=True)
            self.assertEqual(status, 401)
            self.assertEqual(result["status"], "fail")
            self.assertNotIn("user", g)
        self.users.find_one.assert_not_called()

    def test_strict_helper_rejects_token_without_subject(self):
        token = jwt.encode({"other": "claim"}, os.environ["SECRET_KEY"], algorithm="HS256")
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer " + token}):
            result, status = AuthHelper.get_logged_in_user(request, strict=True)
            self.assertEqual((result["status"], status), ("fail", 401))
            self.assertNotIn("user", g)
        self.users.find_one.assert_not_called()

    def test_strict_helper_rejects_structured_subjects_before_mongo_lookup(self):
        for subject in ({"$ne": None}, ["user-123"]):
            with self.subTest(subject=subject):
                token = User.encode_auth_token(subject)
                with self.app.test_request_context("/protected", headers={"Authorization": "Bearer " + token}):
                    result, status = AuthHelper.get_logged_in_user(request, strict=True)
                    self.assertEqual((result["status"], status), ("fail", 401))
                    self.assertNotIn("user", g)
        self.users.find_one.assert_not_called()

    def test_strict_helper_rejects_missing_user_and_clears_stale_context(self):
        token = User.encode_auth_token("user-123")
        self.users.find_one.return_value = None
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer " + token}):
            g.user = "previous authentication"
            result, status = AuthHelper.get_logged_in_user(request, strict=True)
            self.assertEqual((result["status"], status), ("fail", 401))
            self.assertNotIn("user", g)

    def test_strict_helper_keeps_success_fields_and_accepts_bearer_whitespace(self):
        token = User.encode_auth_token("user-123")
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer " + token}):
            legacy = AuthHelper.get_logged_in_user(request)
        with self.app.test_request_context("/protected", headers={"Authorization": "bearer   " + token}):
            self.assertEqual(AuthHelper.get_logged_in_user(request, strict=True), legacy)
            self.assertEqual(g.user.id, "user-123")

    def test_strict_helper_does_not_hide_database_outage(self):
        token = User.encode_auth_token("user-123")
        self.users.find_one.side_effect = RuntimeError("database unavailable")
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer " + token}):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                AuthHelper.get_logged_in_user(request, strict=True)
            self.assertNotIn("user", g)

    def test_strict_decorator_distinguishes_none_empty_and_allowed_roles(self):
        token = User.encode_auth_token("user-123")
        no_grants = get_allowed_roles_for(AppModuleEnum.DATA_ENGINEERING, UserActionEnum.READ)
        self.assertEqual(no_grants, [])
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer " + token}):
            for roles in ([], no_grants, ["ADMIN"]):
                self.assertEqual(token_required(roles, strict=True)(lambda: "allowed")(),
                                 ({"message": "Permission denied"}, 403))
            for roles in (None, ["DATA_OWNER"]):
                self.assertEqual(token_required(roles, strict=True)(lambda: "allowed")(), "allowed")
            self.assertEqual(token_required([])(lambda: "legacy allowed")(), "legacy allowed")

    def test_strict_decorator_returns_existing_invalid_token_envelope(self):
        with self.app.test_request_context("/protected", headers={"Authorization": "Bearer broken"}):
            self.assertEqual(token_required(strict=True)(lambda: "allowed")(),
                             ({"message": "Invalid token"}, 401))


class AuditIdentityTests(contracts.AuthFixture):
    def setUp(self):
        super().setUp()
        self.audit_db = Mock()
        self.audit_db.save.return_value = "audit-1"
        self.enterContext(patch.object(AuditTrail, "db", return_value=self.audit_db))
        self.blueprint = AuditBlueprint("audit", __name__)

    def test_helper_and_mongo_actor_shapes_preserve_id_and_public_fields(self):
        for identity in ({"id": "user-123"}, {"_id": "user-123"},
                         {"id": "user-123", "_id": "different"}):
            with self.app.test_request_context("/items"):
                self.blueprint.create_log("UPDATE", "items", "/items", new_value={"name": "new"},
                                          user_info={**identity, "email": "reader@example.test",
                                                     "full_name": "Reader", "password_hash": "not logged"})
                record = self.audit_db.save.call_args.args[0]
                self.assertEqual(record["user"], {"id": "user-123", "email": "reader@example.test",
                                                 "full_name": "Reader"})
                self.assertEqual(record["collection"], "items")
                self.assertEqual(record["new_value"], {"name": "new"})

    def test_real_audit_hook_keeps_target_collection_when_it_loads_actor(self):
        token = User.encode_auth_token("user-123")
        with self.app.test_request_context("/items/1", method="PUT", json={"name": "new"},
                                          headers={"Authorization": "Bearer " + token}):
            g.table_name = "items"
            g.old_data = {"_id": "item-1", "name": "old"}
            response = self.app.response_class(status=200)
            self.assertIs(self.blueprint.after_data_request(response), response)
            record = self.audit_db.save.call_args.args[0]
            self.assertEqual(record["collection"], "items")
            self.assertEqual(record["user"]["id"], "user-123")
            self.assertEqual(record["old_value"], {"name": "old"})
            self.assertEqual(record["new_value"], {"name": "new", "_id": "item-1"})


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.auth import firebase_auth as firebase_auth_module


class FirebaseAuthTests(unittest.TestCase):
    def test_verify_id_token_rejects_anonymous_provider(self) -> None:
        with patch.object(firebase_auth_module, "_ensure_firebase_app", return_value=True), patch.object(
            firebase_auth_module.firebase_auth,
            "verify_id_token",
            return_value={
                "uid": "anon-user-1",
                "firebase": {"sign_in_provider": "anonymous"},
            },
        ):
            with self.assertRaises(HTTPException) as ctx:
                firebase_auth_module.verify_id_token_value("token-abc")

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "anonymous_auth_disabled")

    def test_verify_id_token_allows_password_provider(self) -> None:
        with patch.object(firebase_auth_module, "_ensure_firebase_app", return_value=True), patch.object(
            firebase_auth_module.firebase_auth,
            "verify_id_token",
            return_value={
                "uid": "user-1",
                "email": "user@example.com",
                "name": "User One",
                "firebase": {"sign_in_provider": "password"},
            },
        ):
            user = firebase_auth_module.verify_id_token_value("token-abc")

        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.uid, "user-1")
        self.assertEqual(user.email, "user@example.com")


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import Mock
from unittest.mock import patch

from fastapi import HTTPException

from app.auth import firebase_auth as firebase_auth_module


class FirebaseAuthTests(unittest.TestCase):
    def test_verify_id_token_rejects_anonymous_provider(self) -> None:
        firebase_auth_mock = Mock()
        firebase_auth_mock.verify_id_token.return_value = {
            "uid": "anon-user-1",
            "firebase": {"sign_in_provider": "anonymous"},
        }
        with patch.object(firebase_auth_module, "_ensure_firebase_app", return_value=True), patch.object(
            firebase_auth_module,
            "firebase_auth",
            firebase_auth_mock,
        ):
            with self.assertRaises(HTTPException) as ctx:
                firebase_auth_module.verify_id_token_value("token-abc")

        self.assertEqual(ctx.exception.status_code, 401)
        self.assertEqual(ctx.exception.detail, "anonymous_auth_disabled")

    def test_verify_id_token_allows_password_provider(self) -> None:
        firebase_auth_mock = Mock()
        firebase_auth_mock.verify_id_token.return_value = {
            "uid": "user-1",
            "email": "user@example.com",
            "name": "User One",
            "firebase": {"sign_in_provider": "password"},
        }
        with patch.object(firebase_auth_module, "_ensure_firebase_app", return_value=True), patch.object(
            firebase_auth_module,
            "firebase_auth",
            firebase_auth_mock,
        ):
            user = firebase_auth_module.verify_id_token_value("token-abc")

        self.assertIsNotNone(user)
        assert user is not None
        self.assertEqual(user.uid, "user-1")
        self.assertEqual(user.email, "user@example.com")

    def test_generate_password_reset_link_falls_back_when_continue_url_domain_not_allowlisted(self) -> None:
        firebase_auth_mock = Mock()
        generate_mock = Mock(
            side_effect=[
                Exception("Error while calling Auth service (UNAUTHORIZED_DOMAIN ). Domain not allowlisted by project"),
                "https://reset-link",
            ]
        )
        firebase_auth_mock.ActionCodeSettings.side_effect = lambda *args, **kwargs: {"args": args, "kwargs": kwargs}
        firebase_auth_mock.generate_password_reset_link = generate_mock
        with patch.object(firebase_auth_module, "_ensure_firebase_app", return_value=True), patch.object(
            firebase_auth_module,
            "firebase_auth",
            firebase_auth_mock,
        ):
            value = firebase_auth_module.generate_password_reset_link_value(
                "member@example.com",
                continue_url="https://www.worshiptranslation.com/login",
            )

        self.assertEqual(value, "https://reset-link")
        self.assertEqual(generate_mock.call_count, 2)


if __name__ == "__main__":
    unittest.main()

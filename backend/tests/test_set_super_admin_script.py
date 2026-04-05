from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import json
from io import StringIO
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from scripts import set_super_admin as set_super_admin_module


class SetSuperAdminScriptTests(unittest.TestCase):
    def _run(self, *args: str) -> tuple[int, str, str]:
        stdout = StringIO()
        stderr = StringIO()
        with patch.object(sys, "argv", ["set_super_admin.py", *args]), redirect_stdout(stdout), redirect_stderr(stderr):
            code = set_super_admin_module.main()
        return code, stdout.getvalue(), stderr.getvalue()

    def test_grant_by_email_resolves_uid_and_sets_super_admin_claim(self) -> None:
        firebase_auth_mock = Mock()
        firebase_auth_mock.get_user_by_email.return_value = SimpleNamespace(
            uid="uid-1",
            email="namjubae@gamil.com",
        )
        firebase_auth_mock.get_user.return_value = SimpleNamespace(
            uid="uid-1",
            email="namjubae@gamil.com",
            custom_claims={"existing": "claim"},
        )

        with patch.object(set_super_admin_module, "_load_backend_env", return_value=None), patch.object(
            set_super_admin_module,
            "_init_firebase",
            return_value=None,
        ), patch.object(set_super_admin_module, "_project_id_from_env", return_value="test-project"), patch.object(
            set_super_admin_module,
            "firebase_auth",
            firebase_auth_mock,
        ):
            code, stdout, stderr = self._run(
                "--grant",
                "--email",
                "namjubae@gamil.com",
                "--actor",
                "ops@example.com",
                "--yes",
            )

        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr.strip(), "")
        event = json.loads(stdout.strip())
        self.assertEqual(event["targetUid"], "uid-1")
        self.assertEqual(event["targetEmail"], "namjubae@gamil.com")
        self.assertTrue(bool(event["afterEnabled"]))
        firebase_auth_mock.get_user_by_email.assert_called_once_with("namjubae@gamil.com")
        firebase_auth_mock.set_custom_user_claims.assert_called_once_with(
            "uid-1",
            {"existing": "claim", "super_admin": True},
        )
        firebase_auth_mock.revoke_refresh_tokens.assert_called_once_with("uid-1")

    def test_status_by_email_returns_structured_error_when_lookup_fails(self) -> None:
        firebase_auth_mock = Mock()
        firebase_auth_mock.get_user_by_email.side_effect = Exception("user not found")

        with patch.object(set_super_admin_module, "_load_backend_env", return_value=None), patch.object(
            set_super_admin_module,
            "_init_firebase",
            return_value=None,
        ), patch.object(set_super_admin_module, "_project_id_from_env", return_value="test-project"), patch.object(
            set_super_admin_module,
            "firebase_auth",
            firebase_auth_mock,
        ):
            code, stdout, stderr = self._run("--status", "--email", "missing@example.com")

        self.assertEqual(code, 1)
        self.assertEqual(stdout.strip(), "")
        event = json.loads(stderr.strip())
        self.assertFalse(bool(event["success"]))
        self.assertEqual(event["targetEmail"], "missing@example.com")
        self.assertIn("user not found", event["error"])

    def test_init_firebase_prefers_firebase_admin_credentials_env(self) -> None:
        firebase_admin_mock = Mock()
        firebase_admin_mock._apps = []
        firebase_credentials_mock = Mock()

        with tempfile.TemporaryDirectory() as tmp_dir:
            firebase_credentials_path = Path(tmp_dir) / "firebase-admin.json"
            google_credentials_path = Path(tmp_dir) / "google-application.json"
            firebase_credentials_path.write_text("{}", encoding="utf-8")
            google_credentials_path.write_text("{}", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "FIREBASE_ADMIN_CREDENTIALS": str(firebase_credentials_path),
                    "GOOGLE_APPLICATION_CREDENTIALS": str(google_credentials_path),
                },
                clear=False,
            ), patch.object(set_super_admin_module, "firebase_admin", firebase_admin_mock), patch.object(
                set_super_admin_module,
                "firebase_auth",
                Mock(),
            ), patch.object(
                set_super_admin_module,
                "firebase_credentials",
                firebase_credentials_mock,
            ):
                set_super_admin_module._init_firebase(project_id="test-project", credentials_path=None)

        firebase_credentials_mock.Certificate.assert_called_once_with(str(firebase_credentials_path.resolve()))
        firebase_admin_mock.initialize_app.assert_called_once()


if __name__ == "__main__":
    unittest.main()

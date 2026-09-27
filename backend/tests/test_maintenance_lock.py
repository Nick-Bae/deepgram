"""Tests for the maintenance-lock refusal at the shared start function.

Covers the R2-approved design:
- Router-level dependency on both start routes refuses BEFORE
  authentication, slug lookup, authorization, billing, or room creation.
- Defense-in-depth call inside ``_start_service_for_org`` catches any
  future direct-import caller that bypasses the routes.
- Parsing is fail-closed: unset / empty / explicit-false unlocks;
  explicit-true and any unknown non-empty value locks.
- Locked response is HTTP 503, ``Retry-After: 60``,
  ``{"detail": "maintenance"}``.
"""
from __future__ import annotations

import logging
import os
import unittest
from typing import Any, Optional
from unittest.mock import patch

from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.auth.firebase_auth import AuthenticatedUser, get_current_user_optional
from app.routes import multichurch as multichurch_routes
from app.routes.multichurch import (
    StartServiceRequest,
    _start_service_for_org,
    require_maintenance_unlocked,
    router as multichurch_router,
)


def _fake_user(uid: str = "namju-owner") -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=f"{uid}@example.com",
        displayName=uid,
        isSuper=False,
    )


def _build_client(*, auth: Optional[AuthenticatedUser] = None) -> TestClient:
    """Bare FastAPI app that mounts only the multichurch router.

    Auth is overridden via FastAPI's ``dependency_overrides`` so tests
    can control whether ``get_current_user_optional`` returns a user.
    ``get_current_user_required`` wraps it, so overriding the optional
    dependency is enough for both routes.
    """
    app = FastAPI()
    app.include_router(multichurch_router, prefix="/api")

    def _override_optional() -> Optional[AuthenticatedUser]:
        return auth

    app.dependency_overrides[get_current_user_optional] = _override_optional
    return TestClient(app, raise_server_exceptions=True)


class _RaisingStore:
    """Sentinel that raises if any store method is called during a test.

    Used to prove that a 503 refusal short-circuits BEFORE any
    downstream call (``list_services``, ``authorize_host``,
    ``start_service``) occurs.
    """

    def __getattr__(self, name: str) -> Any:  # pragma: no cover - defensive
        def _raise(*a: Any, **kw: Any) -> None:
            raise AssertionError(
                f"multichurch_store.{name} MUST NOT be called during "
                "a maintenance refusal (leaked past router dependency)"
            )

        return _raise


class _EnvMaintenance:
    """Context manager that sets/unsets MAINTENANCE_LOCK_ACTIVE."""

    def __init__(self, value: Optional[str]) -> None:
        self._value = value
        self._prev: Optional[str] = None

    def __enter__(self) -> "_EnvMaintenance":
        self._prev = os.environ.get("MAINTENANCE_LOCK_ACTIVE")
        if self._value is None:
            os.environ.pop("MAINTENANCE_LOCK_ACTIVE", None)
        else:
            os.environ["MAINTENANCE_LOCK_ACTIVE"] = self._value
        return self

    def __exit__(self, *exc_info: Any) -> None:
        if self._prev is None:
            os.environ.pop("MAINTENANCE_LOCK_ACTIVE", None)
        else:
            os.environ["MAINTENANCE_LOCK_ACTIVE"] = self._prev


# ---------------------------------------------------------------------------
# L1..L6 — Fail-closed parsing (direct call to require_maintenance_unlocked)
# ---------------------------------------------------------------------------


class ParsingTests(unittest.TestCase):
    def test_L1_env_unset_unlocks(self) -> None:
        with _EnvMaintenance(None):
            self.assertIsNone(require_maintenance_unlocked())

    def test_L2_env_empty_unlocks(self) -> None:
        with _EnvMaintenance(""):
            self.assertIsNone(require_maintenance_unlocked())

    def test_L3_env_whitespace_only_unlocks(self) -> None:
        with _EnvMaintenance("   \t\n "):
            self.assertIsNone(require_maintenance_unlocked())

    def test_L4_env_explicit_false_values_unlock(self) -> None:
        for value in [
            "0",
            "false",
            "no",
            "off",
            "False",
            "NO",
            "OfF",
            "  false  ",
            "TRUE ".strip().lower() and "false",  # noqa: E501 - explicit case coverage
        ]:
            with _EnvMaintenance(value), self.subTest(value=value):
                self.assertIsNone(require_maintenance_unlocked())

    def test_L5_env_explicit_true_values_lock(self) -> None:
        for value in ["1", "true", "yes", "on", "True", "YES", "On", " ON ", "TrUe"]:
            with _EnvMaintenance(value), self.subTest(value=value):
                with self.assertRaises(HTTPException) as ctx:
                    require_maintenance_unlocked()
                self.assertEqual(ctx.exception.status_code, 503)
                self.assertEqual(ctx.exception.detail, "maintenance")
                self.assertEqual(ctx.exception.headers.get("Retry-After"), "60")

    def test_L6_env_unknown_nonempty_values_lock_fail_closed(self) -> None:
        for value in ["foo", "truE1", "2", "abc", "enable", "YESSSS", "0true", "1x", "-1"]:
            with _EnvMaintenance(value), self.subTest(value=value):
                with self.assertRaises(HTTPException) as ctx:
                    require_maintenance_unlocked()
                self.assertEqual(ctx.exception.status_code, 503)
                self.assertEqual(ctx.exception.detail, "maintenance")
                self.assertEqual(ctx.exception.headers.get("Retry-After"), "60")


# ---------------------------------------------------------------------------
# L7..L11 — Route-level check fires before auth / slug lookup / authorize_host
# ---------------------------------------------------------------------------


class RouteLevelRefusalTests(unittest.TestCase):
    def _assert_maintenance_503(self, resp: Any) -> None:
        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.headers.get("Retry-After"), "60")
        self.assertEqual(resp.json(), {"detail": "maintenance"})

    def test_L7_org_route_refused_without_authorization_header(self) -> None:
        with _EnvMaintenance("1"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            client = _build_client(auth=None)
            resp = client.post(
                "/api/org/orgA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
            self._assert_maintenance_503(resp)

    def test_L8_slug_route_refused_without_authorization_header(self) -> None:
        with _EnvMaintenance("1"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            client = _build_client(auth=None)
            resp = client.post(
                "/api/c/slugA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
            self._assert_maintenance_503(resp)

    def test_L9_org_route_refused_with_valid_token_still_503(self) -> None:
        with _EnvMaintenance("1"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            client = _build_client(auth=_fake_user())
            resp = client.post(
                "/api/org/orgA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
            self._assert_maintenance_503(resp)

    def test_L10_auth_dependency_not_reached_under_maintenance(self) -> None:
        # Auth dep raises unconditionally: if it's ever reached, we see 500
        # (or its exception). Maintenance must short-circuit to 503.
        def _boom() -> Optional[AuthenticatedUser]:
            raise RuntimeError("auth-dep-called-under-maintenance")

        app = FastAPI()
        app.include_router(multichurch_router, prefix="/api")
        app.dependency_overrides[get_current_user_optional] = _boom
        client = TestClient(app, raise_server_exceptions=False)
        with _EnvMaintenance("1"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            resp = client.post(
                "/api/org/orgA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.headers.get("Retry-After"), "60")
            self.assertEqual(resp.json(), {"detail": "maintenance"})

    def test_L11_slug_lookup_not_reached_under_maintenance(self) -> None:
        # multichurch_store.list_services raises if called during a
        # maintenance refusal — proves slug lookup is skipped.
        with _EnvMaintenance("1"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            client = _build_client(auth=_fake_user())
            resp = client.post(
                "/api/c/slugA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
            self.assertEqual(resp.status_code, 503)
            self.assertEqual(resp.headers.get("Retry-After"), "60")
            self.assertEqual(resp.json(), {"detail": "maintenance"})


# ---------------------------------------------------------------------------
# L12..L15 — Defense-in-depth on the shared helper
# ---------------------------------------------------------------------------


class SharedHelperDefenseInDepthTests(unittest.TestCase):
    def test_L12_direct_call_refused_under_maintenance(self) -> None:
        with _EnvMaintenance("1"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            with self.assertRaises(HTTPException) as ctx:
                _start_service_for_org(
                    org_id="orgA",
                    service_key="svcA",
                    payload=StartServiceRequest(source="ko", target="en"),
                    current_user=_fake_user(),
                )
            self.assertEqual(ctx.exception.status_code, 503)
            self.assertEqual(ctx.exception.detail, "maintenance")
            self.assertEqual(ctx.exception.headers.get("Retry-After"), "60")

    def test_L13_authorize_host_not_reached_under_maintenance(self) -> None:
        # _RaisingStore.authorize_host would fail the test if called.
        with _EnvMaintenance("1"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            with self.assertRaises(HTTPException):
                _start_service_for_org(
                    org_id="orgA",
                    service_key="svcA",
                    payload=StartServiceRequest(source="ko", target="en"),
                    current_user=_fake_user(),
                )

    def test_L14_start_service_not_reached_under_maintenance(self) -> None:
        # Same _RaisingStore semantics also guard start_service.
        with _EnvMaintenance("1"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            with self.assertRaises(HTTPException):
                _start_service_for_org(
                    org_id="orgA",
                    service_key="svcA",
                    payload=StartServiceRequest(source="ko", target="en"),
                    current_user=_fake_user(),
                )

    def test_L15_security_event_emitted_on_refusal(self) -> None:
        with _EnvMaintenance("1"):
            with self.assertLogs("security", level="WARNING") as cap:
                with self.assertRaises(HTTPException):
                    require_maintenance_unlocked()
        self.assertTrue(
            any('"start_service_refused_maintenance"' in msg for msg in cap.output),
            f"expected security_event; got: {cap.output!r}",
        )
        self.assertTrue(
            any('"maintenance_lock_active_value_class": "explicit_true"' in m
                for m in cap.output),
            f"expected explicit_true class in event; got: {cap.output!r}",
        )


# ---------------------------------------------------------------------------
# L16..L20 — Lock-off normal behavior (both routes + shared helper)
# ---------------------------------------------------------------------------


class _FakeStore:
    """Records that authorize_host / start_service were called; returns
    canned data so the 200 path lands with a JSON body the test asserts."""

    def __init__(self, *, live: bool = True, slug_orgid: str = "orgA") -> None:
        self._live = live
        self._slug_orgid = slug_orgid
        self.calls: list[tuple[str, dict]] = []

    def list_services(self, *, slug: str) -> Optional[dict]:
        self.calls.append(("list_services", {"slug": slug}))
        return {"orgId": self._slug_orgid, "services": []}

    def authorize_host(self, org_id: str, *, host_uid: str, host_token: Any) -> bool:
        self.calls.append(
            ("authorize_host", {"org_id": org_id, "host_uid": host_uid})
        )
        return True

    def start_service(
        self,
        *,
        org_id: str,
        service_key: str,
        host_uid: str,
        source: str,
        target: str,
    ) -> dict:
        self.calls.append(
            (
                "start_service",
                {
                    "org_id": org_id,
                    "service_key": service_key,
                    "host_uid": host_uid,
                    "source": source,
                    "target": target,
                },
            )
        )
        return {
            "orgId": org_id,
            "serviceKey": service_key,
            "roomId": "room-fake",
            "status": "live",
            "languagePair": {"source": source, "target": target},
        }


class LockOffNormalBehaviorTests(unittest.TestCase):
    def test_L16_org_route_lock_unset_normal_200(self) -> None:
        store = _FakeStore()
        with _EnvMaintenance(None), patch.object(
            multichurch_routes, "multichurch_store", store
        ):
            client = _build_client(auth=_fake_user())
            resp = client.post(
                "/api/org/orgA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["status"], "live")
        self.assertEqual(body["orgId"], "orgA")
        self.assertIn(("start_service", {
            "org_id": "orgA",
            "service_key": "svcA",
            "host_uid": "namju-owner",
            "source": "ko",
            "target": "en",
        }), store.calls)

    def test_L17_slug_route_lock_unset_normal_200(self) -> None:
        store = _FakeStore(slug_orgid="orgFromSlug")
        with _EnvMaintenance(None), patch.object(
            multichurch_routes, "multichurch_store", store
        ):
            client = _build_client(auth=_fake_user())
            resp = client.post(
                "/api/c/sluga/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["orgId"], "orgFromSlug")

    def test_L18_org_route_lock_explicit_false_normal_200(self) -> None:
        store = _FakeStore()
        with _EnvMaintenance("0"), patch.object(
            multichurch_routes, "multichurch_store", store
        ):
            client = _build_client(auth=_fake_user())
            resp = client.post(
                "/api/org/orgA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
        self.assertEqual(resp.status_code, 200)

    def test_L19_org_route_lock_explicit_false_case_variants_normal_200(self) -> None:
        for value in ["false", "FALSE", "No", "oFf"]:
            store = _FakeStore()
            with _EnvMaintenance(value), patch.object(
                multichurch_routes, "multichurch_store", store
            ), self.subTest(value=value):
                client = _build_client(auth=_fake_user())
                resp = client.post(
                    "/api/org/orgA/service/svcA/start",
                    json={"source": "ko", "target": "en"},
                )
                self.assertEqual(resp.status_code, 200)

    def test_L20_slug_route_lock_explicit_false_normal_200(self) -> None:
        store = _FakeStore(slug_orgid="orgFromSlug")
        with _EnvMaintenance("0"), patch.object(
            multichurch_routes, "multichurch_store", store
        ):
            client = _build_client(auth=_fake_user())
            resp = client.post(
                "/api/c/sluga/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
        self.assertEqual(resp.status_code, 200)


# ---------------------------------------------------------------------------
# L21..L22 — Flag flipping within a test
# ---------------------------------------------------------------------------


class FlagFlipTests(unittest.TestCase):
    def test_L21_lock_flip_on_then_off_within_test(self) -> None:
        store = _FakeStore()
        # ON: refuse
        with _EnvMaintenance("1"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            client = _build_client(auth=_fake_user())
            resp_on = client.post(
                "/api/org/orgA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
        self.assertEqual(resp_on.status_code, 503)
        # OFF: allow
        with _EnvMaintenance(None), patch.object(
            multichurch_routes, "multichurch_store", store
        ):
            client = _build_client(auth=_fake_user())
            resp_off = client.post(
                "/api/org/orgA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
        self.assertEqual(resp_off.status_code, 200)

    def test_L22_lock_flip_unknown_then_off(self) -> None:
        store = _FakeStore()
        # UNKNOWN value: fail-closed → refuse
        with _EnvMaintenance("oops"), patch.object(
            multichurch_routes, "multichurch_store", _RaisingStore()
        ):
            client = _build_client(auth=_fake_user())
            resp_bad = client.post(
                "/api/org/orgA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
        self.assertEqual(resp_bad.status_code, 503)
        self.assertEqual(resp_bad.headers.get("Retry-After"), "60")
        # OFF (unset): allow
        with _EnvMaintenance(None), patch.object(
            multichurch_routes, "multichurch_store", store
        ):
            client = _build_client(auth=_fake_user())
            resp_ok = client.post(
                "/api/org/orgA/service/svcA/start",
                json={"source": "ko", "target": "en"},
            )
        self.assertEqual(resp_ok.status_code, 200)


if __name__ == "__main__":  # pragma: no cover - manual invocation only
    unittest.main()

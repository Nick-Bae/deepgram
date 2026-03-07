from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.auth.firebase_auth import AuthenticatedUser
from app.routes import auth as auth_routes
from app.routes import multichurch as multichurch_routes
from app.services import multichurch_store as multichurch_store_module
from app.services.multichurch_store import InMemoryMultiChurchStore


class AuthRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMultiChurchStore()
        self.patch_auth_store = patch.object(auth_routes, "multichurch_store", self.store)
        self.patch_multichurch_store = patch.object(multichurch_routes, "multichurch_store", self.store)
        self.patch_auth_store.start()
        self.patch_multichurch_store.start()
        self.addCleanup(self.patch_auth_store.stop)
        self.addCleanup(self.patch_multichurch_store.stop)
        auth_routes._invite_rate_hits.clear()

    @staticmethod
    def _user(uid: str, *, email: str | None = None, display_name: str | None = None) -> AuthenticatedUser:
        return AuthenticatedUser(
            uid=uid,
            email=email or f"{uid}@example.com",
            displayName=display_name or uid,
        )

    def _bootstrap_owner(self, *, uid: str, slug: str, name: str) -> str:
        result = auth_routes.auth_bootstrap_owner(
            auth_routes.BootstrapOwnerRequest(
                churchName=name,
                churchSlug=slug,
                timezone="America/Chicago",
                source="ko",
                target="en",
            ),
            user=self._user(uid),
        )
        org_id = str((result.get("org") or {}).get("orgId") or "").strip()
        self.assertTrue(org_id, result)
        return org_id

    def test_invite_lifecycle_routes_and_second_redeem_conflict(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-route-1", slug="route-life-a", name="Route Life A")

        created = auth_routes.auth_create_invite(
            org_id=org_id,
            payload=auth_routes.CreateInviteRequest(role="viewer"),
            user=self._user("owner-route-1"),
        )
        code = str(created.get("code") or "").strip()
        self.assertTrue(code, created)
        self.assertEqual(created.get("orgId"), org_id)
        self.assertEqual(created.get("role"), "viewer")

        preview = auth_routes.auth_preview_invite(code=code, user=self._user("member-route-1"))
        self.assertEqual(preview.get("orgId"), org_id)
        self.assertFalse(bool(preview.get("alreadyMember")))

        redeemed = auth_routes.auth_redeem_invite(
            code=code,
            user=self._user("member-route-1", email="member-route-1@example.com", display_name="Member Route 1"),
        )
        self.assertEqual(redeemed.get("orgId"), org_id)
        self.assertTrue(bool(redeemed.get("created")))
        self.assertEqual(redeemed.get("currentOrgId"), org_id)
        self.assertNotIn("hostToken", redeemed)

        me = auth_routes.auth_me(user=self._user("member-route-1"))
        self.assertEqual(me.get("currentOrgId"), org_id)

        with self.assertRaises(HTTPException) as second_ctx:
            auth_routes.auth_redeem_invite(
                code=code,
                user=self._user("member-route-2", email="member-route-2@example.com", display_name="Member Route 2"),
            )
        self.assertEqual(second_ctx.exception.status_code, 409)
        self.assertEqual(second_ctx.exception.detail, "invite_invalid")

    def test_set_current_org_route_for_multi_membership_user(self) -> None:
        org_a = self._bootstrap_owner(uid="owner-route-a", slug="route-switch-a", name="Route Switch A")
        org_b = self._bootstrap_owner(uid="owner-route-b", slug="route-switch-b", name="Route Switch B")

        invite_a = auth_routes.auth_create_invite(
            org_id=org_a,
            payload=auth_routes.CreateInviteRequest(role="viewer"),
            user=self._user("owner-route-a"),
        )
        invite_b = auth_routes.auth_create_invite(
            org_id=org_b,
            payload=auth_routes.CreateInviteRequest(role="viewer"),
            user=self._user("owner-route-b"),
        )
        code_a = str(invite_a.get("code") or "").strip()
        code_b = str(invite_b.get("code") or "").strip()
        self.assertTrue(code_a)
        self.assertTrue(code_b)

        redeemed_a = auth_routes.auth_redeem_invite(code=code_a, user=self._user("member-switch-route-1"))
        self.assertEqual(redeemed_a.get("currentOrgId"), org_a)
        redeemed_b = auth_routes.auth_redeem_invite(code=code_b, user=self._user("member-switch-route-1"))
        self.assertEqual(redeemed_b.get("currentOrgId"), org_b)

        switched = auth_routes.auth_set_current_org(
            payload=auth_routes.SetCurrentOrgRequest(orgId=org_a),
            user=self._user("member-switch-route-1"),
        )
        self.assertEqual(switched.get("currentOrgId"), org_a)

        me_after = auth_routes.auth_me(user=self._user("member-switch-route-1"))
        self.assertEqual(me_after.get("currentOrgId"), org_a)

    def test_org_billing_limits_toggle_route_and_permissions(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-route-billing", slug="route-billing-a", name="Route Billing A")

        invite = auth_routes.auth_create_invite(
            org_id=org_id,
            payload=auth_routes.CreateInviteRequest(role="viewer"),
            user=self._user("owner-route-billing"),
        )
        auth_routes.auth_redeem_invite(code=str(invite.get("code") or ""), user=self._user("viewer-route-billing"))

        with self.assertRaises(HTTPException) as forbidden_owner_ctx:
            auth_routes.auth_get_org_billing_limits(org_id=org_id, user=self._user("owner-route-billing"))
        self.assertEqual(forbidden_owner_ctx.exception.status_code, 403)
        self.assertEqual(forbidden_owner_ctx.exception.detail, "billing_admin_required")

        with patch.object(multichurch_store_module, "MASTER_USER_UIDS", {"owner-route-billing"}):
            current = auth_routes.auth_get_org_billing_limits(org_id=org_id, user=self._user("owner-route-billing"))
            self.assertTrue(bool(current.get("billingLimitsEnabled")))

            disabled = auth_routes.auth_set_org_billing_limits(
                org_id=org_id,
                payload=auth_routes.SetOrgBillingLimitsRequest(enabled=False),
                user=self._user("owner-route-billing"),
            )
            self.assertFalse(bool(disabled.get("billingLimitsEnabled")))
            self.assertFalse(bool(disabled.get("effectiveBillingLimitsEnabled")))

        with self.assertRaises(HTTPException) as forbidden_ctx:
            auth_routes.auth_set_org_billing_limits(
                org_id=org_id,
                payload=auth_routes.SetOrgBillingLimitsRequest(enabled=True),
                user=self._user("viewer-route-billing"),
            )
        self.assertEqual(forbidden_ctx.exception.status_code, 403)
        self.assertEqual(forbidden_ctx.exception.detail, "billing_admin_required")

    def test_listener_services_is_public_without_login(self) -> None:
        payload = multichurch_routes.list_services("demo")
        self.assertEqual(payload.get("slug"), "demo")
        self.assertTrue(isinstance(payload.get("services"), list))

    def test_create_invite_rate_limit_returns_429(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-rate-1", slug="route-rate-a", name="Route Rate A")
        with patch.object(auth_routes, "_INVITE_RATE_CREATE_MAX", 1):
            first = auth_routes.auth_create_invite(
                org_id=org_id,
                payload=auth_routes.CreateInviteRequest(role="viewer"),
                user=self._user("owner-rate-1"),
            )
            self.assertTrue(str(first.get("code") or "").strip())

            with self.assertRaises(HTTPException) as second_ctx:
                auth_routes.auth_create_invite(
                    org_id=org_id,
                    payload=auth_routes.CreateInviteRequest(role="viewer"),
                    user=self._user("owner-rate-1"),
                )
            self.assertEqual(second_ctx.exception.status_code, 429)
            self.assertEqual(second_ctx.exception.detail, "invite_rate_limited")

    def test_auth_me_memberships_do_not_expose_host_token(self) -> None:
        org_id = self._bootstrap_owner(uid="owner-route-no-token", slug="route-no-token", name="Route No Token")
        me = auth_routes.auth_me(user=self._user("owner-route-no-token"))
        self.assertEqual(me.get("currentOrgId"), org_id)
        memberships = me.get("memberships") or []
        self.assertTrue(memberships)
        owner_membership = next((row for row in memberships if row.get("orgId") == org_id), None)
        self.assertIsNotNone(owner_membership)
        self.assertNotIn("hostToken", owner_membership or {})

    def test_bootstrap_owner_response_does_not_expose_host_token(self) -> None:
        response = auth_routes.auth_bootstrap_owner(
            auth_routes.BootstrapOwnerRequest(
                churchName="Route No Token Bootstrap",
                churchSlug="route-no-token-bootstrap",
                timezone="America/Chicago",
                source="ko",
                target="en",
            ),
            user=self._user("owner-route-bootstrap-no-token"),
        )
        self.assertNotIn("hostToken", response)
        memberships = response.get("memberships") or []
        self.assertTrue(memberships)
        self.assertTrue(all("hostToken" not in row for row in memberships))


if __name__ == "__main__":
    unittest.main()

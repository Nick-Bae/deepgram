from __future__ import annotations

from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

from app.billing.config import BillingConfig
from app.services import multichurch_store as multichurch_store_module
from app.services.multichurch_store import InMemoryMultiChurchStore


def _bootstrap_owner(
    store: InMemoryMultiChurchStore,
    *,
    owner_uid: str,
    slug: str,
    name: str,
) -> str:
    result = store.bootstrap_owner_org(
        owner_uid=owner_uid,
        owner_email=f"{owner_uid}@example.com",
        owner_display_name=owner_uid,
        church_name=name,
        church_slug=slug,
        timezone="America/Chicago",
        source="ko",
        target="en",
    )
    return str(result["orgId"])


class BillingEntitlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config_patch = patch.object(
            multichurch_store_module,
            "BILLING_CONFIG",
            BillingConfig(
                stripe_secret_key="",
                stripe_webhook_secret="",
                stripe_price_ids={"starter": "", "growth": "", "premium": ""},
                trial_days=30,
                trial_minutes=20,
                grace_days=3,
                entitlements_v2_enabled=True,
            ),
        )
        self.billing_limits_patch = patch.object(multichurch_store_module, "BILLING_LIMITS_ENABLED", True)
        self.config_patch.start()
        self.billing_limits_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.billing_limits_patch.stop)

        self.store = InMemoryMultiChurchStore()
        self.org_id = _bootstrap_owner(
            self.store,
            owner_uid="owner-billing-entitlements",
            slug="billing-entitlements",
            name="Billing Entitlements",
        )

    def _set_billing(self, **updates: object) -> None:
        billing = self.store.get_org_billing_profile(org_id=self.org_id)
        billing.update(updates)
        self.store.set_org_billing_profile(org_id=self.org_id, billing=billing)

    def test_start_service_blocks_when_trial_expired(self) -> None:
        self._set_billing(
            status="trialing",
            trialEndsAt=datetime.now(timezone.utc) - timedelta(minutes=1),
            graceEndsAt=None,
        )
        with self.assertRaisesRegex(PermissionError, "trial_expired"):
            self.store.start_service(
                self.org_id,
                "sun-11am",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )

    def test_start_service_blocks_when_grace_expired(self) -> None:
        self._set_billing(
            status="past_due",
            graceEndsAt=datetime.now(timezone.utc) - timedelta(minutes=1),
        )
        with self.assertRaisesRegex(PermissionError, "grace_expired"):
            self.store.start_service(
                self.org_id,
                "sun-11am",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )

    def test_start_service_blocks_when_subscription_required(self) -> None:
        self._set_billing(status="canceled", graceEndsAt=None)
        with self.assertRaisesRegex(PermissionError, "subscription_required"):
            self.store.start_service(
                self.org_id,
                "sun-11am",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )

    def test_create_service_blocks_when_plan_limit_reached(self) -> None:
        self._set_billing(
            status="active",
            planKey="starter",
            limits={"maxServiceKeys": 3},
            trialEndsAt=None,
            graceEndsAt=None,
        )
        with self.assertRaisesRegex(PermissionError, "plan_limit_reached"):
            self.store.create_service(
                org_id=self.org_id,
                service_key="fri-9pm",
                requested_by_uid="owner-billing-entitlements",
                title="Friday 9 PM",
                timezone="America/Chicago",
                source="ko",
                target="en",
            )

    def test_start_service_requires_existing_service_key(self) -> None:
        self._set_billing(status="active", trialEndsAt=None, graceEndsAt=None)
        with self.assertRaisesRegex(ValueError, "service_not_found"):
            self.store.start_service(
                self.org_id,
                "does-not-exist",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )

    def test_trial_minutes_are_consumed_and_room_gets_flagged_when_exhausted(self) -> None:
        self._set_billing(
            status="trialing",
            planKey="trial",
            trialMinutesLimit=20,
            trialMinutesUsed=19,
            trialEndsAt=datetime.now(timezone.utc) + timedelta(days=7),
            graceEndsAt=None,
        )
        started = self.store.start_service(
            self.org_id,
            "sun-11am",
            host_uid="owner-billing-entitlements",
            source="ko",
            target="en",
        )
        room_id = str(started.get("roomId") or "")
        self.assertTrue(room_id)
        self.store._rooms[(self.org_id, room_id)]["lastUsageTickAt"] = datetime.now(timezone.utc) - timedelta(seconds=120)

        flagged = self.store.enforce_live_usage_caps(tick_seconds=60)
        self.assertTrue(
            any(
                row.get("orgId") == self.org_id
                and row.get("roomId") == room_id
                and row.get("reason") == "trial_expired"
                for row in flagged
            ),
            flagged,
        )

        billing = self.store.get_org_billing_profile(org_id=self.org_id)
        self.assertEqual(int(billing.get("trialMinutesUsed") or 0), 20)
        with self.assertRaisesRegex(PermissionError, "trial_expired"):
            self.store.start_service(
                self.org_id,
                "sun-11am",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )


if __name__ == "__main__":
    unittest.main()

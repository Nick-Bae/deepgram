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
                stripe_price_ids_annual={"starter": "", "growth": "", "premium": ""},
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
        self.assertEqual(int(billing.get("trialSecondsUsed") or 0), 20 * 60)
        with self.assertRaisesRegex(PermissionError, "trial_expired"):
            self.store.start_service(
                self.org_id,
                "sun-11am",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )

    def test_disabled_billing_limits_skip_trial_usage_cap(self) -> None:
        self.store._orgs[self.org_id]["billingLimitsEnabled"] = False
        self._set_billing(
            status="trialing",
            planKey="trial",
            trialMinutesLimit=20,
            trialMinutesUsed=19,
            trialSecondsUsed=19 * 60,
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
        self.store._rooms[(self.org_id, room_id)]["lastUsageTickAt"] = datetime.now(timezone.utc) - timedelta(seconds=300)

        flagged = self.store.enforce_live_usage_caps(tick_seconds=300)
        self.assertEqual(flagged, [])

        billing = self.store.get_org_billing_profile(org_id=self.org_id)
        self.assertEqual(int(billing.get("trialSecondsUsed") or 0), 19 * 60)

    def test_end_room_persists_partial_trial_seconds(self) -> None:
        self._set_billing(
            status="trialing",
            planKey="trial",
            trialMinutesLimit=20,
            trialMinutesUsed=0,
            trialSecondsUsed=0,
            trialEndsAt=datetime.now(timezone.utc) + timedelta(days=7),
            graceEndsAt=None,
        )
        started_at = datetime(2026, 3, 15, 12, 0, 0, tzinfo=timezone.utc)
        with patch.object(multichurch_store_module, "_utcnow", return_value=started_at):
            started = self.store.start_service(
                self.org_id,
                "sun-11am",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )
        room_id = str(started.get("roomId") or "")
        self.assertTrue(room_id)

        room = self.store._rooms[(self.org_id, room_id)]
        room["startedAt"] = started_at
        room["lastUsageTickAt"] = started_at

        ended_at = started_at + timedelta(seconds=10)
        with patch.object(multichurch_store_module, "_utcnow", return_value=ended_at):
            ended = self.store.end_room(self.org_id, room_id, reason="host_end")
        self.assertEqual(ended["status"], "ended")

        billing = self.store.get_org_billing_profile(org_id=self.org_id)
        self.assertEqual(int(billing.get("trialSecondsUsed") or 0), 10)
        self.assertEqual(int(billing.get("trialMinutesUsed") or 0), 0)

        with patch.object(multichurch_store_module, "_utcnow", return_value=ended_at + timedelta(seconds=1)):
            restarted = self.store.start_service(
                self.org_id,
                "sun-11am",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )
        self.assertTrue(str(restarted.get("roomId") or ""))

    def test_end_room_flush_blocks_next_start_when_trial_seconds_are_exhausted(self) -> None:
        self._set_billing(
            status="trialing",
            planKey="trial",
            trialMinutesLimit=20,
            trialMinutesUsed=19,
            trialSecondsUsed=(20 * 60) - 5,
            trialEndsAt=datetime.now(timezone.utc) + timedelta(days=7),
            graceEndsAt=None,
        )
        started_at = datetime(2026, 3, 15, 13, 0, 0, tzinfo=timezone.utc)
        with patch.object(multichurch_store_module, "_utcnow", return_value=started_at):
            started = self.store.start_service(
                self.org_id,
                "sun-11am",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )
        room_id = str(started.get("roomId") or "")
        self.assertTrue(room_id)

        room = self.store._rooms[(self.org_id, room_id)]
        room["startedAt"] = started_at
        room["lastUsageTickAt"] = started_at

        ended_at = started_at + timedelta(seconds=5)
        with patch.object(multichurch_store_module, "_utcnow", return_value=ended_at):
            self.store.end_room(self.org_id, room_id, reason="host_end")

        billing = self.store.get_org_billing_profile(org_id=self.org_id)
        self.assertEqual(int(billing.get("trialSecondsUsed") or 0), 20 * 60)
        self.assertEqual(int(billing.get("trialMinutesUsed") or 0), 20)

        with patch.object(multichurch_store_module, "_utcnow", return_value=ended_at + timedelta(seconds=1)):
            with self.assertRaisesRegex(PermissionError, "trial_expired"):
                self.store.start_service(
                    self.org_id,
                    "sun-11am",
                    host_uid="owner-billing-entitlements",
                    source="ko",
                    target="en",
                )

    def test_flat_firestore_billing_fields_are_lifted_into_billing_payload(self) -> None:
        payload, changed = multichurch_store_module._billing_payload_from_org_row(
            {
                "billing": {
                    "planKey": "trial",
                    "trialMinutesLimit": 20,
                    "trialMinutesUsed": 0,
                },
                "billing.trialSecondsUsed": 17,
                "billing.updatedAt": datetime(2026, 3, 15, 14, 0, 0, tzinfo=timezone.utc),
            }
        )
        self.assertTrue(changed)
        self.assertEqual(payload["planKey"], "trial")
        self.assertEqual(int(payload["trialMinutesLimit"]), 20)
        self.assertEqual(int(payload["trialSecondsUsed"]), 17)
        self.assertIsInstance(payload.get("updatedAt"), datetime)

    def test_nested_billing_values_beat_legacy_flat_firestore_fields(self) -> None:
        payload, changed = multichurch_store_module._billing_payload_from_org_row(
            {
                "billing": {
                    "planKey": "trial",
                    "trialMinutesLimit": 30,
                    "trialMinutesUsed": 1,
                    "trialSecondsUsed": 83,
                },
                "billing.trialMinutesUsed": 1,
                "billing.trialSecondsUsed": 60,
            }
        )
        self.assertTrue(changed)
        self.assertEqual(int(payload["trialSecondsUsed"]), 83)
        self.assertEqual(int(payload["trialMinutesUsed"]), 1)

    def test_effective_trial_seconds_remaining_includes_live_room_runtime(self) -> None:
        self._set_billing(
            status="trialing",
            planKey="trial",
            trialMinutesLimit=20,
            trialMinutesUsed=0,
            trialSecondsUsed=0,
            trialEndsAt=datetime.now(timezone.utc) + timedelta(days=7),
            graceEndsAt=None,
        )
        started_at = datetime(2026, 3, 15, 15, 0, 0, tzinfo=timezone.utc)
        with patch.object(multichurch_store_module, "_utcnow", return_value=started_at):
            started = self.store.start_service(
                self.org_id,
                "sun-11am",
                host_uid="owner-billing-entitlements",
                source="ko",
                target="en",
            )
        room_id = str(started.get("roomId") or "")
        self.assertTrue(room_id)
        room = self.store._rooms[(self.org_id, room_id)]
        room["startedAt"] = started_at
        room["lastUsageTickAt"] = started_at

        check_at = started_at + timedelta(seconds=17)
        with patch.object(multichurch_store_module, "_utcnow", return_value=check_at):
            remaining = self.store.get_org_effective_trial_seconds_remaining(org_id=self.org_id)

        self.assertEqual(remaining, (20 * 60) - 17)


if __name__ == "__main__":
    unittest.main()

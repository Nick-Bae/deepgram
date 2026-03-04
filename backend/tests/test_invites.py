from __future__ import annotations

import unittest

from app.services.multichurch_store import (
    INVITE_STATUS_ACTIVE,
    InMemoryMultiChurchStore,
)


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


class InviteFlowTests(unittest.TestCase):
    def test_invite_lifecycle_create_preview_redeem_and_second_redeem_fails(self) -> None:
        store = InMemoryMultiChurchStore()
        org_id = _bootstrap_owner(
            store,
            owner_uid="owner-invite-1",
            slug="invite-lifecycle-org",
            name="Invite Lifecycle Church",
        )

        created = store.create_invite(
            org_id=org_id,
            created_by_uid="owner-invite-1",
            role="viewer",
            expires_in_hours=24,
        )

        self.assertEqual(created["orgId"], org_id)
        self.assertEqual(created["role"], "viewer")
        self.assertEqual(created["status"], INVITE_STATUS_ACTIVE)
        self.assertIsInstance(created["code"], str)
        self.assertTrue(created["code"])

        preview = store.preview_invite(code=created["code"], uid="member-invite-1")
        self.assertEqual(preview["orgId"], org_id)
        self.assertEqual(preview["role"], "viewer")
        self.assertFalse(preview["alreadyMember"])

        redeemed = store.redeem_invite(
            code=created["code"],
            uid="member-invite-1",
            email="member-invite-1@example.com",
            display_name="Invite Member 1",
        )
        self.assertEqual(redeemed["orgId"], org_id)
        self.assertEqual(redeemed["role"], "viewer")
        self.assertTrue(redeemed["created"])
        self.assertFalse(redeemed["alreadyMember"])
        self.assertEqual(redeemed["currentOrgId"], org_id)
        self.assertEqual(store.get_current_org_id("member-invite-1"), org_id)

        with self.assertRaisesRegex(ValueError, "invite_invalid"):
            store.redeem_invite(
                code=created["code"],
                uid="member-invite-1",
                email="member-invite-1@example.com",
                display_name="Invite Member 1",
            )

    def test_set_current_org_for_multi_membership_user(self) -> None:
        store = InMemoryMultiChurchStore()
        org_a = _bootstrap_owner(
            store,
            owner_uid="owner-switch-a",
            slug="switch-org-a",
            name="Switch Org A",
        )
        org_b = _bootstrap_owner(
            store,
            owner_uid="owner-switch-b",
            slug="switch-org-b",
            name="Switch Org B",
        )

        invite_a = store.create_invite(
            org_id=org_a,
            created_by_uid="owner-switch-a",
            role="viewer",
            expires_in_hours=24,
        )
        invite_b = store.create_invite(
            org_id=org_b,
            created_by_uid="owner-switch-b",
            role="viewer",
            expires_in_hours=24,
        )

        store.redeem_invite(
            code=invite_a["code"],
            uid="member-switch-1",
            email="member-switch-1@example.com",
            display_name="Member Switch",
        )
        store.redeem_invite(
            code=invite_b["code"],
            uid="member-switch-1",
            email="member-switch-1@example.com",
            display_name="Member Switch",
        )

        self.assertEqual(store.get_current_org_id("member-switch-1"), org_b)
        selected = store.set_current_org("member-switch-1", org_a)
        self.assertEqual(selected, org_a)
        self.assertEqual(store.get_current_org_id("member-switch-1"), org_a)

        with self.assertRaisesRegex(PermissionError, "org_access_denied"):
            store.set_current_org("outsider-user", org_a)


if __name__ == "__main__":
    unittest.main()

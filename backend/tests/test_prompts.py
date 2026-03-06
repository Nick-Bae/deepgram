from __future__ import annotations

import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app.auth.firebase_auth import AuthenticatedUser
from app.routes import prompt as prompt_routes
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


def _user(uid: str) -> AuthenticatedUser:
    return AuthenticatedUser(uid=uid, email=f"{uid}@example.com", displayName=uid)


class PromptRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = InMemoryMultiChurchStore()
        self.patch_store = patch.object(prompt_routes, "multichurch_store", self.store)
        self.patch_store.start()
        self.addCleanup(self.patch_store.stop)

    def test_org_prompt_is_scoped_per_church(self) -> None:
        org_a = _bootstrap_owner(self.store, owner_uid="owner-prompt-a", slug="prompt-org-a", name="Prompt Org A")
        org_b = _bootstrap_owner(self.store, owner_uid="owner-prompt-b", slug="prompt-org-b", name="Prompt Org B")

        saved_a = prompt_routes.set_org_prompt(
            org_id=org_a,
            payload=prompt_routes.PromptPayload(prompt="A global", service_prompt="A service"),
            user=_user("owner-prompt-a"),
        )
        saved_b = prompt_routes.set_org_prompt(
            org_id=org_b,
            payload=prompt_routes.PromptPayload(prompt="B global", service_prompt="B service"),
            user=_user("owner-prompt-b"),
        )

        self.assertEqual(saved_a.get("prompt"), "A global")
        self.assertEqual(saved_b.get("prompt"), "B global")

        loaded_a = prompt_routes.get_org_prompt(org_id=org_a, user=_user("owner-prompt-a"))
        loaded_b = prompt_routes.get_org_prompt(org_id=org_b, user=_user("owner-prompt-b"))

        self.assertEqual(loaded_a.get("prompt"), "A global")
        self.assertEqual(loaded_a.get("service_prompt"), "A service")
        self.assertEqual(loaded_b.get("prompt"), "B global")
        self.assertEqual(loaded_b.get("service_prompt"), "B service")

        tx_a = self.store.get_org_prompt_for_translation(org_a)
        tx_b = self.store.get_org_prompt_for_translation(org_b)
        self.assertEqual(tx_a.get("prompt"), "A global")
        self.assertEqual(tx_b.get("prompt"), "B global")

    def test_non_member_cannot_access_other_church_prompt(self) -> None:
        org_a = _bootstrap_owner(self.store, owner_uid="owner-prompt-deny", slug="prompt-deny-a", name="Prompt Deny A")
        _bootstrap_owner(self.store, owner_uid="owner-prompt-deny-b", slug="prompt-deny-b", name="Prompt Deny B")

        with self.assertRaises(HTTPException) as read_ctx:
            prompt_routes.get_org_prompt(org_id=org_a, user=_user("owner-prompt-deny-b"))
        self.assertEqual(read_ctx.exception.status_code, 403)

        with self.assertRaises(HTTPException) as write_ctx:
            prompt_routes.set_org_prompt(
                org_id=org_a,
                payload=prompt_routes.PromptPayload(prompt="bad", service_prompt="bad"),
                user=_user("owner-prompt-deny-b"),
            )
        self.assertEqual(write_ctx.exception.status_code, 403)


if __name__ == "__main__":
    unittest.main()

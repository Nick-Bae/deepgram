from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException

from app.auth.firebase_auth import AuthenticatedUser
from app.routes import script as script_routes
from app.services.multichurch_store import InMemoryMultiChurchStore
from app.services.script_store import ScriptStore


def _user(uid: str) -> AuthenticatedUser:
    return AuthenticatedUser(uid=uid, email=f"{uid}@example.com", displayName=uid)


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


class ScriptRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.members = InMemoryMultiChurchStore()
        self.scripts = ScriptStore()
        self.patch_members = patch.object(script_routes, "multichurch_store", self.members)
        self.patch_scripts = patch.object(script_routes, "script_store", self.scripts)
        self.patch_members.start()
        self.patch_scripts.start()
        self.addCleanup(self.patch_members.stop)
        self.addCleanup(self.patch_scripts.stop)

    def test_script_upload_is_isolated_per_org(self) -> None:
        org_a = _bootstrap_owner(self.members, owner_uid="owner-script-a", slug="script-org-a", name="Script Org A")
        org_b = _bootstrap_owner(self.members, owner_uid="owner-script-b", slug="script-org-b", name="Script Org B")

        uploaded = script_routes.upload_script(
            org_id=org_a,
            body=script_routes.UploadPayload(
                payload={"pairs": [{"source": "하나님은 선하십니다", "target": "God is good"}]},
                cfg={"threshold": 0.9},
            ),
            user=_user("owner-script-a"),
        )
        self.assertEqual(uploaded.get("loaded"), 1)
        self.assertEqual(uploaded.get("threshold"), 0.9)

        stats_a = script_routes.script_status(org_id=org_a, user=_user("owner-script-a"))
        stats_b = script_routes.script_status(org_id=org_b, user=_user("owner-script-b"))
        self.assertEqual(stats_a.get("count"), 1)
        self.assertEqual(stats_b.get("count"), 0)

    def test_script_matching_handles_spacing_punctuation_and_containment(self) -> None:
        org_id = _bootstrap_owner(self.members, owner_uid="owner-script-fuzzy", slug="script-fuzzy", name="Script Fuzzy")
        script_routes.upload_script(
            org_id=org_id,
            body=script_routes.UploadPayload(
                payload={
                    "pairs": [
                        {
                            "source": "오늘 본문은 역대하 마지막 장입니다.",
                            "target": "Today's passage is the final chapter of 2 Chronicles.",
                        }
                    ]
                },
                cfg={"threshold": 0.8},
            ),
            user=_user("owner-script-fuzzy"),
        )

        punctuation_variant = "오늘 본문은  역대하 마지막 장입니다"
        hit_a, score_a, _, _ = self.scripts.match(punctuation_variant, org_id=org_id)
        self.assertIsNotNone(hit_a)
        self.assertGreaterEqual(score_a, 0.8)

        contained_variant = "사랑하는 성도 여러분, 오늘 본문은 역대하 마지막 장입니다 그리고 은혜를 나눕니다"
        hit_b, score_b, _, _ = self.scripts.match(contained_variant, org_id=org_id)
        self.assertIsNotNone(hit_b)
        self.assertGreaterEqual(score_b, 0.8)

        duplicated_stt_variant = "오늘 본문은 역대하 마지막 장입니다 오늘본문은역대하마지막장입니다"
        hit_c, score_c, _, _ = self.scripts.match(duplicated_stt_variant, org_id=org_id)
        self.assertIsNotNone(hit_c)
        self.assertGreaterEqual(score_c, 0.8)

    def test_script_upload_requires_membership_and_role(self) -> None:
        org_a = _bootstrap_owner(self.members, owner_uid="owner-script-c", slug="script-org-c", name="Script Org C")
        _bootstrap_owner(self.members, owner_uid="owner-script-d", slug="script-org-d", name="Script Org D")

        with self.assertRaises(HTTPException) as outsider_ctx:
            script_routes.upload_script(
                org_id=org_a,
                body=script_routes.UploadPayload(payload={"pairs": [{"source": "a", "target": "b"}]}),
                user=_user("owner-script-d"),
            )
        self.assertEqual(outsider_ctx.exception.status_code, 403)
        self.assertEqual(outsider_ctx.exception.detail, "org_access_denied")

        invite = self.members.create_invite(
            org_id=org_a,
            created_by_uid="owner-script-c",
            role="viewer",
            expires_in_hours=24,
        )
        self.members.redeem_invite(
            code=invite["code"],
            uid="viewer-script-c",
            email="viewer-script-c@example.com",
            display_name="Viewer Script C",
        )
        with self.assertRaises(HTTPException) as viewer_ctx:
            script_routes.upload_script(
                org_id=org_a,
                body=script_routes.UploadPayload(payload={"pairs": [{"source": "a", "target": "b"}]}),
                user=_user("viewer-script-c"),
            )
        self.assertEqual(viewer_ctx.exception.status_code, 403)
        self.assertEqual(viewer_ctx.exception.detail, "forbidden")

    def test_sermon_draft_splits_and_translates(self) -> None:
        org_id = _bootstrap_owner(self.members, owner_uid="owner-sermon-a", slug="sermon-org-a", name="Sermon Org A")

        async def _run() -> dict:
            with patch.object(
                script_routes,
                "translate_text",
                new=AsyncMock(side_effect=lambda text, *_args, **_kwargs: f"EN::{text}"),
            ):
                return await script_routes.draft_sermon(
                    org_id=org_id,
                    body=script_routes.SermonDraftRequest(
                        sermon_id="2026-03-08-am",
                        korean="오늘 본문은 역대하 마지막 장입니다. 하나님은 신실하십니다.",
                        auto_split=True,
                        threshold=0.8,
                        lang_src="ko",
                        lang_tgt="en",
                    ),
                    user=_user("owner-sermon-a"),
                )

        drafted = asyncio.run(_run())
        segments = drafted.get("segments") or []
        self.assertEqual(drafted.get("sermon_id"), "2026-03-08-am")
        self.assertEqual(len(segments), 2)
        self.assertEqual(segments[0]["id"], 1)
        self.assertEqual(segments[0]["ko"], "오늘 본문은 역대하 마지막 장입니다.")
        self.assertEqual(segments[0]["en"], "EN::오늘 본문은 역대하 마지막 장입니다.")

    def test_sermon_finalize_saves_sermon_and_pairs(self) -> None:
        org_id = _bootstrap_owner(self.members, owner_uid="owner-sermon-b", slug="sermon-org-b", name="Sermon Org B")
        finalized = script_routes.finalize_sermon(
            org_id=org_id,
            body=script_routes.SermonFinalizeRequest(
                sermon_id="2026-03-08-pm",
                threshold=0.81,
                lang_src="ko",
                lang_tgt="en",
                segments=[
                    script_routes.SermonSegment(
                        id=1,
                        ko="오늘 본문은 역대하의 마지막 장입니다.",
                        en="Today's passage is the final chapter of 2 Chronicles.",
                    ),
                    script_routes.SermonSegment(
                        id=2,
                        ko="하나님은 끝까지 신실하십니다.",
                        en="God remains faithful to the end.",
                    ),
                ],
            ),
            user=_user("owner-sermon-b"),
        )

        self.assertTrue(finalized.get("saved"))
        self.assertEqual(finalized.get("loaded"), 2)
        self.assertEqual(finalized.get("threshold"), 0.81)

        hit, score, _, threshold = self.scripts.match(
            "오늘 본문은 역대하의 마지막 장입니다.",
            org_id=org_id,
        )
        self.assertIsNotNone(hit)
        self.assertGreater(score, 0.99)
        self.assertAlmostEqual(threshold, 0.81)

        stored = self.scripts.get_sermon("2026-03-08-pm", org_id=org_id)
        self.assertIsNotNone(stored)
        self.assertEqual((stored or {}).get("sermon_id"), "2026-03-08-pm")
        self.assertEqual(len((stored or {}).get("segments") or []), 2)

    def test_default_script_routes_use_current_org(self) -> None:
        owner_uid = "owner-script-default"
        org_a = _bootstrap_owner(self.members, owner_uid=owner_uid, slug="script-default-a", name="Script Default A")
        org_b_owner = "owner-script-default-b"
        org_b = _bootstrap_owner(self.members, owner_uid=org_b_owner, slug="script-default-b", name="Script Default B")
        invite_b = self.members.create_invite(
            org_id=org_b,
            created_by_uid=org_b_owner,
            role="host",
            expires_in_hours=24,
        )
        self.members.redeem_invite(
            code=invite_b["code"],
            uid=owner_uid,
            email=f"{owner_uid}@example.com",
            display_name=owner_uid,
        )
        self.members.set_current_org(owner_uid, org_b)

        uploaded = script_routes.upload_script_default_org(
            body=script_routes.UploadPayload(
                payload={"pairs": [{"source": "은혜", "target": "grace"}]},
                cfg={"threshold": 0.87},
            ),
            user=_user(owner_uid),
        )
        self.assertEqual(uploaded.get("loaded"), 1)
        self.assertEqual(uploaded.get("threshold"), 0.87)

        stats_default = script_routes.script_status_default_org(user=_user(owner_uid))
        stats_b = script_routes.script_status(org_id=org_b, user=_user(owner_uid))
        stats_a = script_routes.script_status(org_id=org_a, user=_user(owner_uid))
        self.assertEqual(stats_default.get("count"), 1)
        self.assertEqual(stats_b.get("count"), 1)
        self.assertEqual(stats_a.get("count"), 0)

    def test_default_sermon_routes_use_current_org(self) -> None:
        owner_uid = "owner-sermon-default"
        org_a = _bootstrap_owner(self.members, owner_uid=owner_uid, slug="sermon-default-a", name="Sermon Default A")
        org_b_owner = "owner-sermon-default-b"
        org_b = _bootstrap_owner(self.members, owner_uid=org_b_owner, slug="sermon-default-b", name="Sermon Default B")
        invite_b = self.members.create_invite(
            org_id=org_b,
            created_by_uid=org_b_owner,
            role="host",
            expires_in_hours=24,
        )
        self.members.redeem_invite(
            code=invite_b["code"],
            uid=owner_uid,
            email=f"{owner_uid}@example.com",
            display_name=owner_uid,
        )
        self.members.set_current_org(owner_uid, org_b)

        async def _run() -> tuple[dict, dict]:
            with patch.object(
                script_routes,
                "translate_text",
                new=AsyncMock(side_effect=lambda text, *_args, **_kwargs: f"EN::{text}"),
            ):
                drafted = await script_routes.draft_sermon_default_org(
                    body=script_routes.SermonDraftRequest(
                        sermon_id="2026-03-08-am",
                        korean="오늘 본문입니다. 하나님은 신실하십니다.",
                        auto_split=True,
                    ),
                    user=_user(owner_uid),
                )
            finalized = script_routes.finalize_sermon_default_org(
                body=script_routes.SermonFinalizeRequest(
                    sermon_id=drafted["sermon_id"],
                    threshold=0.8,
                    lang_src="ko",
                    lang_tgt="en",
                    segments=[script_routes.SermonSegment(**row) for row in drafted["segments"]],
                ),
                user=_user(owner_uid),
            )
            return drafted, finalized

        drafted, finalized = asyncio.run(_run())
        self.assertEqual(len(drafted.get("segments") or []), 2)
        self.assertTrue(finalized.get("saved"))

        stored_b = self.scripts.get_sermon("2026-03-08-am", org_id=org_b)
        stored_a = self.scripts.get_sermon("2026-03-08-am", org_id=org_a)
        self.assertIsNotNone(stored_b)
        self.assertIsNone(stored_a)


if __name__ == "__main__":
    unittest.main()

"""L1 API tests for routes/slides.py (Design Ref: §8.2).

Pattern matches existing repo tests (test_billing_routes.py): direct async
handler invocation with module-level patches. Avoids FastAPI TestClient
overhead and keeps tests focused on route logic.
"""
from __future__ import annotations

import asyncio
import io
import unittest
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

from fastapi import HTTPException, UploadFile

from app.auth.firebase_auth import AuthenticatedUser
from app.routes import slides as slides_routes


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _make_user(uid: str = "host-uid-1", *, super_user: bool = False) -> AuthenticatedUser:
    return AuthenticatedUser(
        uid=uid,
        email=f"{uid}@example.com",
        displayName="Test Host",
        isSuper=super_user,
    )


def _make_request() -> Any:
    """Minimal stand-in for FastAPI Request — only needs .client.host for IP logging."""
    req = MagicMock()
    req.client.host = "127.0.0.1"
    req.headers = {}
    return req


def _make_png(width: int = 320, height: int = 240) -> bytes:
    """Build a tiny valid PNG with the requested dimensions via Pillow."""
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(64, 128, 192)).save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg(width: int = 320, height: int = 240) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(255, 64, 64)).save(buf, format="JPEG")
    return buf.getvalue()


def _make_upload_file(name: str, data: bytes, content_type: str) -> UploadFile:
    return UploadFile(filename=name, file=io.BytesIO(data), headers={"content-type": content_type})


# ─────────────────────────────────────────────────────────────────────────────
# Fake store + bucket + ws_manager
# ─────────────────────────────────────────────────────────────────────────────

class _FakeStore:
    """In-memory fake of multichurch_store with just the slides + auth surface."""

    def __init__(self, *, memberships: Optional[Dict[str, str]] = None):
        # uid -> {orgId -> role}
        self._memberships = memberships or {"host-uid-1": {"org-1": "host"}}
        # (orgId, serviceKey) -> { "slides": [...], "currentSlideIndex": int|None, "slidesVisibility": str }
        self._states: Dict[tuple, Dict[str, Any]] = {}

    # auth
    def list_memberships(self, uid: str) -> List[Dict[str, Any]]:
        rows = []
        for org_id, role in (self._memberships.get(uid) or {}).items():
            rows.append({"orgId": org_id, "role": role})
        return rows

    def get_current_org_id(self, uid: str) -> Optional[str]:
        for row in self.list_memberships(uid):
            return row.get("orgId")
        return None

    # slides
    def _state_for(self, org_id: str, service_key: str) -> Dict[str, Any]:
        key = (org_id, service_key)
        if key not in self._states:
            self._states[key] = {"slides": [], "currentSlideIndex": None, "slidesVisibility": "private"}
        return self._states[key]

    def get_slide_state(self, *, org_id: str = "", service_key: str = "", **_) -> Dict[str, Any]:
        state = self._state_for(org_id, service_key)
        return {
            "currentSlideIndex": state["currentSlideIndex"],
            "slides": [dict(s) for s in state["slides"]],
            "slideCount": len(state["slides"]),
            "slidesVisibility": state["slidesVisibility"],
        }

    def list_slides(self, org_id: str, service_key: str) -> List[Dict[str, Any]]:
        return [dict(s) for s in self._state_for(org_id, service_key)["slides"]]

    def get_slide(self, *, org_id: str = "", service_key: str = "", slide_id: str = "") -> Optional[Dict[str, Any]]:
        for s in self._state_for(org_id, service_key)["slides"]:
            if s["slideId"] == slide_id:
                return dict(s)
        return None

    def add_slide(self, *, org_id: str = "", service_key: str = "", slide: Dict[str, Any]) -> Dict[str, Any]:
        state = self._state_for(org_id, service_key)
        record = dict(slide)
        state["slides"].append(record)
        return dict(record)

    def update_slide(self, *, org_id: str = "", service_key: str = "", slide_id: str = "", updates: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        for s in self._state_for(org_id, service_key)["slides"]:
            if s["slideId"] == slide_id:
                s.update(updates)
                return dict(s)
        return None

    def delete_slide(self, *, org_id: str = "", service_key: str = "", slide_id: str = "") -> bool:
        state = self._state_for(org_id, service_key)
        before = len(state["slides"])
        state["slides"] = [s for s in state["slides"] if s["slideId"] != slide_id]
        return len(state["slides"]) < before

    def reorder_slides(self, *, org_id: str = "", service_key: str = "", ordered_slide_ids: List[str]) -> List[Dict[str, Any]]:
        state = self._state_for(org_id, service_key)
        lookup = {s["slideId"]: s for s in state["slides"]}
        reordered = []
        for i, sid in enumerate(ordered_slide_ids):
            if sid in lookup:
                lookup[sid]["order"] = i
                reordered.append(lookup[sid])
        state["slides"] = reordered
        return [dict(s) for s in reordered]

    def set_current_slide_index(self, *, org_id: str = "", service_key: str = "", index: int) -> int:
        state = self._state_for(org_id, service_key)
        state["currentSlideIndex"] = int(index)
        return int(index)

    def get_active_room(self, org_id: str, service_key: str) -> Optional[str]:
        return "room-live-1"

    def resolve_service(self, slug: str, service_key: str) -> Optional[Dict[str, Any]]:
        return {"orgId": "org-1", "slug": slug, "serviceKey": service_key}


class _FakeBlob:
    def __init__(self, path: str):
        self.path = path
        self.uploaded: bytes | None = None
        self.content_type: str | None = None
        self.cache_control: str | None = None
        self.public_url = f"https://fake-bucket.test/{path}"
        self.media_link = f"https://fake-bucket.test/media/{path}"

    def upload_from_string(self, data: bytes, content_type: str = "", timeout: int = 30):
        self.uploaded = data
        self.content_type = content_type

    def patch(self):
        pass

    def delete(self, timeout: int = 15):
        pass

    def make_public(self):
        pass

    def generate_signed_url(self, **kwargs) -> str:
        return f"https://fake-bucket.test/signed/{self.path}"


class _FakeBucket:
    def __init__(self):
        self.blobs: Dict[str, _FakeBlob] = {}

    def blob(self, path: str) -> _FakeBlob:
        if path not in self.blobs:
            self.blobs[path] = _FakeBlob(path)
        return self.blobs[path]


class _FakeWsManager:
    def __init__(self):
        self.calls: List[Dict[str, Any]] = []

    async def broadcast_slide_change(self, org_id: str, room_id: str, payload: Dict[str, Any]) -> int:
        self.calls.append({"orgId": org_id, "roomId": room_id, "payload": dict(payload)})
        return 2  # pretend two viewers received it


# ─────────────────────────────────────────────────────────────────────────────
# Test base class — patches module-level singletons
# ─────────────────────────────────────────────────────────────────────────────

class _SlidesRouteTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.store = _FakeStore()
        self.bucket = _FakeBucket()
        self.ws = _FakeWsManager()

        self.patches = [
            patch.object(slides_routes, "multichurch_store", self.store),
            patch.object(slides_routes, "ws_manager", self.ws),
            patch.object(slides_routes, "_bucket", lambda: self.bucket),
        ]
        # require_org_role lives in app.auth.guards but is bound into slides_routes;
        # it reads the store passed to it, so patching multichurch_store covers auth too.
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

class ListSlidesTests(_SlidesRouteTestBase):
    def test_returns_empty_list_for_new_service(self):
        result = _run(
            slides_routes.list_slides(
                request=_make_request(),
                org_id="org-1",
                service_key="svc-sun",
                user=_make_user(),
            )
        )
        self.assertEqual(result["data"]["slides"], [])
        self.assertIsNone(result["data"]["currentSlideIndex"])

    def test_blocks_non_member(self):
        viewer_user = _make_user("outsider-uid")
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.list_slides(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    user=viewer_user,
                )
            )
        self.assertEqual(cm.exception.status_code, 403)


class UploadSlidesTests(_SlidesRouteTestBase):
    def test_uploads_valid_png(self):
        png = _make_png()
        upload = _make_upload_file("hello.png", png, "image/png")
        result = _run(
            slides_routes.upload_slides(
                request=_make_request(),
                org_id="org-1",
                service_key="svc-sun",
                files=[upload],
                user=_make_user(),
            )
        )
        slides = result["data"]["slides"]
        self.assertEqual(len(slides), 1)
        slide = slides[0]
        self.assertTrue(slide["slideId"])
        self.assertEqual(slide["contentType"], "image/png")
        self.assertEqual(slide["order"], 0)
        # Storage path uses the documented layout (Design §3.3).
        self.assertTrue(slide["storagePath"].startswith("orgs/org-1/services/svc-sun/slides/"))
        self.assertTrue(slide["storagePath"].endswith(".png"))
        # URL was attached.
        self.assertIn("url", slide)
        self.assertTrue(slide["url"])

    def test_uploads_valid_jpeg(self):
        jpeg = _make_jpeg()
        upload = _make_upload_file("photo.jpg", jpeg, "image/jpeg")
        result = _run(
            slides_routes.upload_slides(
                request=_make_request(),
                org_id="org-1",
                service_key="svc-sun",
                files=[upload],
                user=_make_user(),
            )
        )
        slide = result["data"]["slides"][0]
        self.assertEqual(slide["contentType"], "image/jpeg")
        self.assertTrue(slide["storagePath"].endswith(".jpg"))

    def test_rejects_pdf_via_magic_bytes(self):
        pdf = b"%PDF-1.4\n" + b"\x00" * 200
        upload = _make_upload_file("doc.pdf", pdf, "application/pdf")
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.upload_slides(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    files=[upload],
                    user=_make_user(),
                )
            )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.detail["code"], "INVALID_FILE_TYPE")

    def test_rejects_oversized_file(self):
        # Build a file that exceeds the cap but is still a valid PNG header.
        big = _make_png() + (b"\x00" * (slides_routes.MAX_SLIDE_IMAGE_BYTES + 100))
        upload = _make_upload_file("huge.png", big, "image/png")
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.upload_slides(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    files=[upload],
                    user=_make_user(),
                )
            )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.detail["code"], "FILE_TOO_LARGE")

    def test_rejects_empty_file(self):
        upload = _make_upload_file("empty.png", b"", "image/png")
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.upload_slides(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    files=[upload],
                    user=_make_user(),
                )
            )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.detail["code"], "EMPTY_FILE")

    def test_blocks_non_host_role(self):
        # User is a viewer in this org — has membership but not host/admin/owner.
        self.store._memberships["viewer-uid"] = {"org-1": "viewer"}
        png = _make_png()
        upload = _make_upload_file("hello.png", png, "image/png")
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.upload_slides(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    files=[upload],
                    user=_make_user("viewer-uid"),
                )
            )
        self.assertEqual(cm.exception.status_code, 403)

    def test_enforces_slide_count_cap(self):
        # Pre-fill the store right at the cap.
        for i in range(slides_routes.MAX_SLIDES_PER_SERVICE):
            self.store.add_slide(
                org_id="org-1",
                service_key="svc-sun",
                slide={
                    "slideId": f"existing-{i}",
                    "order": i,
                    "storagePath": f"p/{i}.png",
                    "contentType": "image/png",
                    "byteSize": 100,
                    "width": 320,
                    "height": 240,
                },
            )
        upload = _make_upload_file("one-too-many.png", _make_png(), "image/png")
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.upload_slides(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    files=[upload],
                    user=_make_user(),
                )
            )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.detail["code"], "SLIDE_LIMIT_EXCEEDED")


class DeleteSlideTests(_SlidesRouteTestBase):
    def test_deletes_existing_slide_and_storage_object(self):
        self.store.add_slide(
            org_id="org-1",
            service_key="svc-sun",
            slide={
                "slideId": "slide-A",
                "order": 0,
                "storagePath": "orgs/org-1/services/svc-sun/slides/slide-A.png",
                "contentType": "image/png",
                "byteSize": 100,
                "width": 320,
                "height": 240,
            },
        )
        result = _run(
            slides_routes.delete_slide(
                request=_make_request(),
                org_id="org-1",
                service_key="svc-sun",
                slide_id="slide-A",
                user=_make_user(),
            )
        )
        self.assertTrue(result["data"]["deleted"])
        self.assertEqual(self.store.list_slides("org-1", "svc-sun"), [])

    def test_returns_404_for_unknown_slide(self):
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.delete_slide(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    slide_id="missing",
                    user=_make_user(),
                )
            )
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(cm.exception.detail["code"], "SLIDE_NOT_FOUND")


class ReorderSlidesTests(_SlidesRouteTestBase):
    def test_reorders_slides(self):
        for sid in ("a", "b", "c"):
            self.store.add_slide(
                org_id="org-1",
                service_key="svc-sun",
                slide={
                    "slideId": sid,
                    "order": 0,
                    "storagePath": f"p/{sid}.png",
                    "contentType": "image/png",
                    "byteSize": 100,
                    "width": 320,
                    "height": 240,
                },
            )
        body = slides_routes.SlideOrderUpdate(orderedSlideIds=["c", "a", "b"])
        result = _run(
            slides_routes.reorder_slides(
                request=_make_request(),
                org_id="org-1",
                service_key="svc-sun",
                body=body,
                user=_make_user(),
            )
        )
        ids = [s["slideId"] for s in result["data"]["slides"]]
        self.assertEqual(ids, ["c", "a", "b"])
        for i, slide in enumerate(result["data"]["slides"]):
            self.assertEqual(slide["order"], i)


class SetSlideIndexTests(_SlidesRouteTestBase):
    def _seed_slides(self, count: int = 3):
        for i in range(count):
            self.store.add_slide(
                org_id="org-1",
                service_key="svc-sun",
                slide={
                    "slideId": f"sid-{i}",
                    "order": i,
                    "storagePath": f"p/{i}.png",
                    "contentType": "image/png",
                    "byteSize": 100,
                    "width": 320,
                    "height": 240,
                },
            )

    def test_advances_to_valid_index_and_broadcasts(self):
        self._seed_slides(3)
        body = slides_routes.SlideIndexUpdate(index=1, roomId="room-live-1")
        result = _run(
            slides_routes.set_slide_index(
                request=_make_request(),
                org_id="org-1",
                service_key="svc-sun",
                body=body,
                user=_make_user(),
            )
        )
        self.assertEqual(result["data"]["currentSlideIndex"], 1)
        self.assertEqual(result["data"]["broadcastedTo"], 2)
        # Broadcast was called with the right payload shape.
        self.assertEqual(len(self.ws.calls), 1)
        call = self.ws.calls[0]
        self.assertEqual(call["orgId"], "org-1")
        self.assertEqual(call["roomId"], "room-live-1")
        self.assertEqual(call["payload"]["index"], 1)
        self.assertEqual(call["payload"]["slideId"], "sid-1")

    def test_rejects_out_of_range_index(self):
        self._seed_slides(2)
        body = slides_routes.SlideIndexUpdate(index=5)
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.set_slide_index(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    body=body,
                    user=_make_user(),
                )
            )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.detail["code"], "INDEX_OUT_OF_RANGE")

    def test_rejects_when_no_slides_uploaded(self):
        body = slides_routes.SlideIndexUpdate(index=0)
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.set_slide_index(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    body=body,
                    user=_make_user(),
                )
            )
        self.assertEqual(cm.exception.status_code, 400)
        self.assertEqual(cm.exception.detail["code"], "NO_SLIDES")


class UpdateSlideTests(_SlidesRouteTestBase):
    def test_updates_caption(self):
        self.store.add_slide(
            org_id="org-1",
            service_key="svc-sun",
            slide={
                "slideId": "sid-1",
                "order": 0,
                "storagePath": "p/sid-1.png",
                "contentType": "image/png",
                "byteSize": 100,
                "width": 320,
                "height": 240,
            },
        )
        body = slides_routes.SlideUpdate(caption="Opening hymn")
        result = _run(
            slides_routes.update_slide(
                request=_make_request(),
                org_id="org-1",
                service_key="svc-sun",
                slide_id="sid-1",
                body=body,
                user=_make_user(),
            )
        )
        self.assertEqual(result["data"]["slide"]["caption"], "Opening hymn")

    def test_returns_404_for_unknown_slide(self):
        body = slides_routes.SlideUpdate(caption="x")
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.update_slide(
                    request=_make_request(),
                    org_id="org-1",
                    service_key="svc-sun",
                    slide_id="missing",
                    body=body,
                    user=_make_user(),
                )
            )
        self.assertEqual(cm.exception.status_code, 404)
        self.assertEqual(cm.exception.detail["code"], "SLIDE_NOT_FOUND")


class PublicStateTests(_SlidesRouteTestBase):
    def test_returns_state_for_public_slug(self):
        self.store.add_slide(
            org_id="org-1",
            service_key="svc-sun",
            slide={
                "slideId": "sid-1",
                "order": 0,
                "storagePath": "p/sid-1.png",
                "contentType": "image/png",
                "byteSize": 100,
                "width": 320,
                "height": 240,
            },
        )
        self.store.set_current_slide_index(org_id="org-1", service_key="svc-sun", index=0)
        result = _run(
            slides_routes.get_slide_state_public(
                request=_make_request(),
                slug="firstchurch",
                service_key="svc-sun",
            )
        )
        data = result["data"]
        self.assertEqual(data["currentSlideIndex"], 0)
        self.assertEqual(len(data["slides"]), 1)
        self.assertEqual(data["slides"][0]["slideId"], "sid-1")
        self.assertIn("url", data["slides"][0])

    def test_returns_404_for_unknown_service(self):
        # Override resolve_service to return None.
        self.store.resolve_service = lambda slug, service_key: None  # type: ignore[assignment]
        with self.assertRaises(HTTPException) as cm:
            _run(
                slides_routes.get_slide_state_public(
                    request=_make_request(),
                    slug="missing",
                    service_key="svc-x",
                )
            )
        self.assertEqual(cm.exception.status_code, 404)


class HelperFunctionTests(unittest.TestCase):
    def test_detect_image_type_recognises_png_and_jpeg(self):
        self.assertEqual(slides_routes._detect_image_type(_make_png()), "image/png")
        self.assertEqual(slides_routes._detect_image_type(_make_jpeg()), "image/jpeg")

    def test_detect_image_type_rejects_unknown(self):
        self.assertIsNone(slides_routes._detect_image_type(b"%PDF-1.4 not an image"))
        self.assertIsNone(slides_routes._detect_image_type(b""))

    def test_strip_exif_preserves_dimensions(self):
        png = _make_png(800, 600)
        stripped, w, h = slides_routes._strip_exif_and_get_size(png, "image/png")
        self.assertEqual((w, h), (800, 600))
        self.assertGreater(len(stripped), 0)


if __name__ == "__main__":
    unittest.main()

"""Seed the Firestore emulator with the minimum org / service / room
records the backend expects. Writes go through the same
`FirestoreMultiChurchStore` code paths the backend uses at runtime, so
schema drift is impossible.

Used from tests to (a) create the church + service before A hosts a
room, and (b) read back room status independently of A / B — the
emulator is the shared source of truth."""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional


def build_admin_store(
    *,
    emulator_host: str = "127.0.0.1:8085",
    project_id: str = "cleanup-track1-harness",
):
    """Instantiate a FirestoreMultiChurchStore bound to the emulator.

    Must be called AFTER the emulator env vars are set in the parent
    process — the google-cloud-firestore client picks them up at
    import time.
    """
    os.environ["FIRESTORE_EMULATOR_HOST"] = emulator_host
    os.environ["GOOGLE_CLOUD_PROJECT"] = project_id
    os.environ.setdefault("MULTICHURCH_STORE_BACKEND", "firestore")
    from app.services import multichurch_store as store_mod
    import importlib
    importlib.reload(store_mod)
    return store_mod.FirestoreMultiChurchStore()


def seed_org_and_service(
    store,
    *,
    org_id: str,
    slug: str,
    service_key: str,
    host_token: str = "harness-host-token",
    e2e_host_uid: Optional[str] = None,
) -> None:
    """Write a minimal org + service into the emulator.

    If `e2e_host_uid` is given, also seed a `members/<uid>` document
    with role=host so `authorize_host(host_uid=e2e_host_uid)` returns
    True — which is what the End Service HTTP endpoint needs when a
    test bearer token bootstraps a stub authenticated user via
    `firebase_auth.verify_id_token_value`.
    """
    now = datetime.now(tz=timezone.utc)
    # Direct writes against the store's Firestore client — the
    # normal signup HTTP path expects Firebase auth we don't have
    # in this harness.
    store._org_ref(org_id).set({
        "id": org_id,
        "slug": slug,
        "name": f"Harness Church ({org_id})",
        "plan": "starter",
        "billing": {"planKey": "starter"},
        "hostToken": host_token,
        "status": "active",
        "billingLimitsEnabled": False,
        "hardCapReached": False,
        "softCapReached": False,
        "createdAt": now,
        "updatedAt": now,
    })
    store._service_ref(org_id, service_key).set({
        "orgId": org_id,
        "serviceKey": service_key,
        "slug": service_key,
        "title": "Harness Service",
        "activeRoomId": None,
        "lastRoomId": None,
        "createdAt": now,
        "updatedAt": now,
    })
    if e2e_host_uid:
        store._org_ref(org_id).collection("members").document(e2e_host_uid).set({
            "uid": e2e_host_uid,
            "role": "host",
            "createdAt": now,
            "updatedAt": now,
        })


def start_room(
    store,
    *,
    org_id: str,
    service_key: str,
    room_id: str = "harness-room-1",
) -> str:
    """Write a live room into the emulator. Returns the room id."""
    now = datetime.now(tz=timezone.utc)
    store._room_ref(org_id, room_id).set({
        "serviceKey": service_key,
        "status": "live",
        "startedAt": now,
        "endedAt": None,
        "hostUid": "harness-host-uid",
        "languagePair": {"source": "ko", "target": "en"},
        "listenerCountPeak": 0,
        "billingPeriodKey": f"{now.year:04d}{now.month:02d}",
        "endReason": None,
        "lastAudioAt": now,
        "lastUsageTickAt": now,
        "finalTranscript": "",
    })
    store._service_ref(org_id, service_key).set(
        {"activeRoomId": room_id, "updatedAt": now},
        merge=True,
    )
    return room_id


def read_room(
    store,
    *,
    org_id: str,
    room_id: str,
    timeout: float = 3.0,
) -> Optional[Dict[str, Any]]:
    """Read the room's Firestore state via the emulator.

    `timeout` is passed straight through to the Firestore client's
    per-RPC timeout. That bounds the underlying gRPC call — not just
    the async await on the wrapper. Wrapping this in
    `asyncio.wait_for(asyncio.to_thread(...))` cancels the WORKER
    but leaves the RPC socket stalled, so the RPC timeout is the
    real safety net.

    Returns None if the room does not exist.
    """
    snap = store._room_ref(org_id, room_id).get(timeout=timeout)
    if not snap.exists:
        return None
    return snap.to_dict()

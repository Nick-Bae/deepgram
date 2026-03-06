
# backend/app/main.py
import os, json, asyncio, logging, time, re
from typing import Optional, Any
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# --- local modules (single import each) ---
from app.socket_manager import manager
from app.deepgram_session import connect_to_deepgram
from app.services.script_store import script_store
from app.services.multichurch_store import multichurch_store
from app.utils.translate import translate_text, TranslationContext  # async wrapper you already have
from app.scripture import detect_scripture_verse
from app.routes import translate as translate_routes  # your existing REST routes
from app.routes import examples as examples_routes
from app.routes import script as script_routes
from app.routes import prompt as prompt_routes
from app.routes import multichurch as multichurch_routes
from app.routes import auth as auth_routes
from app.auth.firebase_auth import verify_id_token_value
from app.chunker.ko_chunker import KoChunker

# Global display pacing config (broadcast to display clients)
APP_DISPLAY_SPEED = {"speed": 1.0}
ROOM_IDLE_TIMEOUT_SEC = int(os.getenv("ROOM_IDLE_TIMEOUT_SEC", "900"))  # 15 min
ROOM_MAX_DURATION_SEC = int(os.getenv("ROOM_MAX_DURATION_SEC", "10800"))  # 3 hours
ROOM_SWEEPER_INTERVAL_SEC = int(os.getenv("ROOM_SWEEPER_INTERVAL_SEC", "60"))
ROOM_USAGE_TICK_SEC = int(os.getenv("ROOM_USAGE_TICK_SEC", "300"))  # 5 min
_room_sweeper_task: asyncio.Task | None = None

# ------------------------------------------------------------------------------
# ONE app only
# ------------------------------------------------------------------------------
app = FastAPI(title="Real-Time Translation Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # relax for dev; tighten for prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Keep your existing HTTP routes under /api
app.include_router(translate_routes.router, prefix="/api")
app.include_router(examples_routes.router, prefix="/api")
app.include_router(script_routes.router, prefix="/api")
app.include_router(prompt_routes.router, prefix="/api")
app.include_router(multichurch_routes.router, prefix="/api")
app.include_router(auth_routes.router, prefix="/api")

@app.get("/")
def root():
    return {"ok": True, "msg": "server is live"}


def _clean_token(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    txt = str(raw).strip()
    return txt or None


def _resolve_room_context(
    *,
    org_id: Optional[str],
    room_id: Optional[str],
    service_key: Optional[str],
    church_slug: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    org = _clean_token(org_id)
    room = _clean_token(room_id)
    service = _clean_token(service_key)
    slug = _clean_token(church_slug)
    if org and room:
        return org, room

    if org and service:
        active = multichurch_store.get_active_room(org, service)
        if active:
            return org, active

    if slug and service:
        resolved = multichurch_store.resolve_service(slug=slug, service_key=service)
        if resolved and resolved.get("orgId") and resolved.get("activeRoomId"):
            return str(resolved["orgId"]), str(resolved["activeRoomId"])
    return org, None


def _prompt_overrides_for_org(org_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    clean_org_id = _clean_token(org_id)
    if not clean_org_id:
        return None, None
    try:
        data = multichurch_store.get_org_prompt_for_translation(clean_org_id) or {}
    except Exception:
        return "", ""
    return str(data.get("prompt") or ""), str(data.get("service_prompt") or "")


def _context_from_query_params(query_params) -> dict[str, Optional[str]]:
    org_id = _clean_token(query_params.get("orgId") or query_params.get("org_id"))
    room_id = _clean_token(query_params.get("roomId") or query_params.get("room_id"))
    service_key = _clean_token(query_params.get("serviceKey") or query_params.get("service_key"))
    church_slug = _clean_token(query_params.get("churchSlug") or query_params.get("church_slug") or query_params.get("slug"))
    role = _clean_token(query_params.get("role"))
    host_token = _clean_token(query_params.get("hostToken") or query_params.get("host_token") or query_params.get("token"))
    host_uid = _clean_token(query_params.get("hostUid") or query_params.get("host_uid") or query_params.get("uid"))
    id_token = _clean_token(query_params.get("idToken") or query_params.get("id_token"))
    return {
        "orgId": org_id,
        "roomId": room_id,
        "serviceKey": service_key,
        "churchSlug": church_slug,
        "role": role,
        "hostToken": host_token,
        "hostUid": host_uid,
        "idToken": id_token,
    }


def _host_claims_from_payload(payload: dict[str, Any], base_ctx: dict[str, Optional[str]]) -> tuple[Optional[str], Optional[str], Optional[str]]:
    host_token = _clean_token(
        payload.get("hostToken")
        or payload.get("host_token")
        or payload.get("token")
        or base_ctx.get("hostToken")
    )
    host_uid = _clean_token(
        payload.get("hostUid")
        or payload.get("host_uid")
        or payload.get("uid")
        or base_ctx.get("hostUid")
    )
    id_token = _clean_token(
        payload.get("idToken")
        or payload.get("id_token")
        or base_ctx.get("idToken")
    )
    return host_uid, host_token, id_token


def _uid_from_id_token(raw_token: Optional[str]) -> Optional[str]:
    token = _clean_token(raw_token)
    if not token:
        return None
    try:
        user = verify_id_token_value(token)
    except Exception:
        return None
    return _clean_token(user.uid) if user else None


def _can_host(org_id: Optional[str], *, host_uid: Optional[str], host_token: Optional[str]) -> bool:
    clean_org = _clean_token(org_id)
    if not clean_org:
        return False
    try:
        return bool(
            multichurch_store.authorize_host(
                clean_org,
                host_uid=_clean_token(host_uid),
                host_token=_clean_token(host_token),
            )
        )
    except Exception:
        return False


async def _room_sweeper_loop() -> None:
    while True:
        try:
            await asyncio.sleep(max(15, ROOM_SWEEPER_INTERVAL_SEC))
            stale_rooms = multichurch_store.stale_live_rooms(
                idle_seconds=max(60, ROOM_IDLE_TIMEOUT_SEC),
                max_duration_seconds=max(600, ROOM_MAX_DURATION_SEC),
            )
            cap_rooms = multichurch_store.enforce_live_usage_caps(
                tick_seconds=max(60, ROOM_USAGE_TICK_SEC),
            )
            for room in stale_rooms + cap_rooms:
                org_id = _clean_token(room.get("orgId"))
                room_id = _clean_token(room.get("roomId"))
                reason = _clean_token(room.get("reason")) or "idle_timeout"
                if not org_id or not room_id:
                    continue
                try:
                    result = multichurch_store.end_room(org_id, room_id, reason=reason)
                    if result.get("alreadyEnded"):
                        continue
                except ValueError:
                    continue
                except Exception as exc:
                    print(f"[ROOM_SWEEPER] end_room failed org={org_id} room={room_id} err={exc}")
                    continue
                try:
                    await manager.broadcast_room(
                        org_id,
                        room_id,
                        {
                            "type": "STATUS",
                            "orgId": org_id,
                            "roomId": room_id,
                            "roomStatus": "ended",
                            "viewerCount": 0,
                            "reason": reason,
                            "message": "Monthly limit reached. Please contact your admin." if reason == "monthly_limit_reached" else None,
                        },
                    )
                except Exception:
                    pass
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[ROOM_SWEEPER] loop error: {exc}")


@app.on_event("startup")
async def _on_startup():
    global _room_sweeper_task
    print(f"[MULTICHURCH] store={type(multichurch_store).__name__}")
    if _room_sweeper_task is None or _room_sweeper_task.done():
        _room_sweeper_task = asyncio.create_task(_room_sweeper_loop())


@app.on_event("shutdown")
async def _on_shutdown():
    global _room_sweeper_task
    if _room_sweeper_task and not _room_sweeper_task.done():
        _room_sweeper_task.cancel()
        try:
            await _room_sweeper_task
        except asyncio.CancelledError:
            pass
    _room_sweeper_task = None

# ------------------------------------------------------------------------------
# Consumer hub: /ws/translate
#  - Frontend connects here (useTranslationSocket)
#  - Stays connected; usually sends only {"type":"consumer_join"}
# ------------------------------------------------------------------------------
@app.websocket("/ws/translate")
async def ws_translate(ws: WebSocket):
    await manager.connect(ws)
    display_config = {"type": "display_config", "speed": APP_DISPLAY_SPEED["speed"]}
    try:
        await ws.send_json(display_config)
    except Exception:
        pass
    translation_ctx = TranslationContext()
    seq = 0
    qctx = _context_from_query_params(ws.query_params)
    joined_org_id, joined_room_id = _resolve_room_context(
        org_id=qctx.get("orgId"),
        room_id=qctx.get("roomId"),
        service_key=qctx.get("serviceKey"),
        church_slug=qctx.get("churchSlug"),
    )
    claimed_role = (qctx.get("role") or "listener").strip().lower()
    joined_role = claimed_role if claimed_role in {"host", "listener", "viewer"} else "listener"
    host_uid_claim = _uid_from_id_token(qctx.get("idToken")) or qctx.get("hostUid")
    host_token_claim = qctx.get("hostToken")
    host_authed = False
    joined_service_key = qctx.get("serviceKey")
    joined_church_slug = qctx.get("churchSlug")
    prompt_overrides_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}

    def _cached_prompt_overrides(org_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        clean_org_id = _clean_token(org_id)
        if not clean_org_id:
            return None, None
        cached = prompt_overrides_cache.get(clean_org_id)
        if cached is not None:
            return cached
        resolved = _prompt_overrides_for_org(clean_org_id)
        prompt_overrides_cache[clean_org_id] = resolved
        return resolved

    async def _broadcast_status_for_joined_room() -> None:
        if not joined_org_id or not joined_room_id:
            return
        viewer_count = manager.room_viewer_count(joined_org_id, joined_room_id)
        try:
            multichurch_store.bump_listener_peak(joined_org_id, joined_room_id, viewer_count)
        except Exception:
            pass
        await manager.broadcast_room(
            joined_org_id,
            joined_room_id,
            {
                "type": "STATUS",
                "orgId": joined_org_id,
                "roomId": joined_room_id,
                "roomStatus": "live",
                "viewerCount": viewer_count,
            },
        )

    if joined_org_id and joined_room_id:
        if joined_role == "host":
            host_authed = _can_host(joined_org_id, host_uid=host_uid_claim, host_token=host_token_claim)
            if not host_authed:
                joined_role = "listener"
        viewer_count = manager.join_room(ws, joined_org_id, joined_room_id, joined_role)
        try:
            multichurch_store.bump_listener_peak(joined_org_id, joined_room_id, viewer_count)
        except Exception:
            pass
        try:
            await ws.send_json(
                {
                    "type": "JOINED",
                    "orgId": joined_org_id,
                    "roomId": joined_room_id,
                    "serviceKey": joined_service_key,
                    "viewerCount": viewer_count,
                    "role": joined_role,
                    "hostAuth": host_authed,
                }
            )
        except Exception:
            pass
        try:
            await _broadcast_status_for_joined_room()
        except Exception:
            pass

    async def handle_commit(payload: dict, is_partial: bool = False):
        nonlocal seq, joined_org_id, joined_room_id, joined_service_key, joined_church_slug, host_uid_claim, host_token_claim, host_authed
        src_text = (payload.get("text") or "").strip()
        if not src_text:
            return

        ws_role = manager.get_role(ws)
        host_uid, host_token, id_token = _host_claims_from_payload(payload, qctx)
        verified_uid = _uid_from_id_token(id_token)
        if verified_uid:
            host_uid_claim = verified_uid
        elif host_uid:
            host_uid_claim = host_uid
        if host_token:
            host_token_claim = host_token

        active_org_id = joined_org_id or _clean_token(payload.get("orgId") or payload.get("org_id"))
        if ws_role != "host":
            try:
                await ws.send_json({"type": "error", "message": "host_role_required"})
            except Exception:
                pass
            return
        if not host_authed:
            host_authed = _can_host(active_org_id, host_uid=host_uid_claim, host_token=host_token_claim)
        if not host_authed:
            try:
                await ws.send_json({"type": "error", "message": "host_auth_failed"})
            except Exception:
                pass
            return

        src_lang_full = _clean_lang(payload.get("source"), "ko")
        tgt_lang_full = _clean_lang(payload.get("target"), "en")
        src_lang = _normalize_lang(src_lang_full, "ko")
        tgt_lang = _normalize_lang(tgt_lang_full, "en")

        payload_org_id = _clean_token(payload.get("orgId") or payload.get("org_id")) or joined_org_id
        payload_room_id = _clean_token(payload.get("roomId") or payload.get("room_id")) or joined_room_id
        payload_service_key = _clean_token(payload.get("serviceKey") or payload.get("service_key")) or joined_service_key
        payload_church_slug = _clean_token(payload.get("churchSlug") or payload.get("church_slug") or payload.get("slug")) or joined_church_slug
        target_org_id, target_room_id = _resolve_room_context(
            org_id=payload_org_id,
            room_id=payload_room_id,
            service_key=payload_service_key,
            church_slug=payload_church_slug,
        )

        seq += 1
        meta_payload = {
            "mode": "realtime",
            "partial": is_partial,
            "segment_id": payload.get("id") or seq,
            "rev": payload.get("rev") or 0,
            "seq": seq,
            "is_final": not is_partial and bool(payload.get("final", True)),
            "producer": True,
        }
        if target_org_id:
            meta_payload["org_id"] = target_org_id
        if target_room_id:
            meta_payload["room_id"] = target_room_id
        if payload_service_key:
            meta_payload["service_key"] = payload_service_key

        script_match, match_score, script_version, script_threshold = script_store.match(
            src_text,
            org_id=target_org_id,
        )
        live_mode = "live"

        if script_match:
            translated = script_match.target
            live_mode = "pre"
            meta_payload.update(
                {
                    "mode": "pre",
                    "match_score": round(match_score, 4),
                    "matched_source": script_match.source,
                    "source_text": script_match.source,
                    "source_version": f"pre-script@{script_version}",
                    "threshold": script_threshold,
                }
            )
            translation_ctx.last_english = translated
        elif src_lang == tgt_lang and src_lang_full == tgt_lang_full:
            translated = src_text
        else:
            try:
                custom_prompt, service_prompt = _cached_prompt_overrides(target_org_id)
                translated = await translate_text(
                    src_text,
                    src_lang_full,
                    tgt_lang_full,
                    ctx=translation_ctx,
                    custom_prompt=custom_prompt,
                    service_prompt=service_prompt,
                )
                translation_ctx.last_english = translated
            except Exception as exc:
                print("[WS translate][producer_commit][error]", exc)
                translated = src_text
                meta_payload.update(_fail_open_meta(exc))

        live_msg_new = {
            "mode": live_mode if not is_partial else "realtime",
            "text": translated,
            "seq": seq,
            "src": {"text": src_text, "lang": src_lang_full},
            "tgt": {"lang": tgt_lang_full},
            "meta": meta_payload.copy(),
        }
        if target_org_id:
            live_msg_new["orgId"] = target_org_id
        if target_room_id:
            live_msg_new["roomId"] = target_room_id

        live_msg_legacy = {
            "type": "translation",
            "payload": translated,
            "lang": tgt_lang_full,
            "meta": meta_payload.copy(),
        }

        try:
            if target_org_id and target_room_id:
                await manager.broadcast_room(target_org_id, target_room_id, live_msg_new)
                await manager.broadcast_room(target_org_id, target_room_id, live_msg_legacy)
            else:
                await manager.broadcast(live_msg_new)
                await manager.broadcast(live_msg_legacy)
        except Exception as exc:
            print("[WS translate][broadcast][error]", exc)

        try:
            await ws.send_json(live_msg_legacy)
        except Exception:
            pass

    try:
        while True:
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            if not isinstance(msg, dict):
                continue

            mtype = str(msg.get("type") or "").strip()
            mtype_l = mtype.lower()
            if mtype_l in {"consumer_join", "join"}:
                joined_service_key = _clean_token(msg.get("serviceKey") or msg.get("service_key")) or joined_service_key
                joined_church_slug = _clean_token(msg.get("churchSlug") or msg.get("church_slug") or msg.get("slug")) or joined_church_slug
                req_org_id = _clean_token(msg.get("orgId") or msg.get("org_id")) or joined_org_id
                req_room_id = _clean_token(msg.get("roomId") or msg.get("room_id")) or joined_room_id
                host_uid, host_token, id_token = _host_claims_from_payload(msg, qctx)
                verified_uid = _uid_from_id_token(id_token)
                if verified_uid:
                    host_uid_claim = verified_uid
                elif host_uid:
                    host_uid_claim = host_uid
                if host_token:
                    host_token_claim = host_token
                resolved_org_id, resolved_room_id = _resolve_room_context(
                    org_id=req_org_id,
                    room_id=req_room_id,
                    service_key=joined_service_key,
                    church_slug=joined_church_slug,
                )
                if resolved_org_id and resolved_room_id:
                    joined_org_id = resolved_org_id
                    joined_room_id = resolved_room_id
                    requested_role = (_clean_token(msg.get("role")) or joined_role or "listener").strip().lower()
                    joined_role = requested_role if requested_role in {"host", "listener", "viewer"} else "listener"
                    if joined_role == "host":
                        host_authed = _can_host(
                            joined_org_id,
                            host_uid=host_uid_claim,
                            host_token=host_token_claim,
                        )
                        if not host_authed:
                            joined_role = "listener"
                    else:
                        host_authed = False
                    viewer_count = manager.join_room(ws, joined_org_id, joined_room_id, joined_role)
                    try:
                        multichurch_store.bump_listener_peak(joined_org_id, joined_room_id, viewer_count)
                    except Exception:
                        pass
                    await _broadcast_status_for_joined_room()
                    try:
                        await ws.send_json(
                            {
                                "type": "JOINED",
                                "orgId": joined_org_id,
                                "roomId": joined_room_id,
                                "serviceKey": joined_service_key,
                                "viewerCount": viewer_count,
                                "role": joined_role,
                                "hostAuth": host_authed,
                            }
                        )
                    except Exception:
                        pass
                else:
                    try:
                        await ws.send_json({"type": "STATUS", "roomStatus": "waiting", "viewerCount": 0})
                    except Exception:
                        pass
                continue
            if mtype_l == "display_config":
                if manager.get_role(ws) != "host" or not host_authed:
                    try:
                        await ws.send_json({"type": "error", "message": "host_auth_required"})
                    except Exception:
                        pass
                    continue
                raw_speed = msg.get("speed")
                if raw_speed is None:
                    raw_speed = msg.get("speedFactor")
                try:
                    speed = float(raw_speed)
                except (TypeError, ValueError):
                    continue
                speed = max(0.6, min(1.6, speed))
                APP_DISPLAY_SPEED["speed"] = speed
                try:
                    if joined_org_id and joined_room_id:
                        await manager.broadcast_room(joined_org_id, joined_room_id, {"type": "display_config", "speed": speed})
                    else:
                        await manager.broadcast({"type": "display_config", "speed": speed})
                except Exception:
                    pass
                continue
            if mtype_l == "producer_commit":
                await handle_commit(msg, is_partial=False)
                continue
            if mtype_l == "producer_partial":
                await handle_commit(msg, is_partial=True)
                continue
    finally:
        room_before = manager.get_room(ws)
        manager.disconnect(ws)
        if room_before:
            org_id, room_id = room_before
            viewer_count = manager.room_viewer_count(org_id, room_id)
            try:
                await manager.broadcast_room(
                    org_id,
                    room_id,
                    {
                        "type": "STATUS",
                        "orgId": org_id,
                        "roomId": room_id,
                        "roomStatus": "live",
                        "viewerCount": viewer_count,
                    },
                )
            except Exception:
                pass

# ------------------------------------------------------------------------------
# Producer: /ws/stt/deepgram
#  - Browser streams PCM → backend → Deepgram
#  - We show partials to producer (for the textarea)
#  - On Deepgram is_final=True, we translate and broadcast to all consumers
# ------------------------------------------------------------------------------
# ---- replace your entire ws_stt_deepgram with this ----

def _clean_lang(raw: Optional[str], default: str) -> str:
    if raw is None:
        return default
    cleaned = raw.strip().lower()
    return cleaned or default


def _normalize_lang(raw: Optional[str], default: str) -> str:
    if not raw:
        return default
    cleaned = raw.strip().lower()
    if not cleaned:
        return default
    primary = cleaned.split("-")[0]
    return primary or default


def _fail_open_meta(exc: Exception) -> dict[str, Any]:
    """
    Build a small meta payload explaining why we fell back to echoing the source text.
    Kept minimal so it can be shown safely in the UI.
    """
    msg = str(exc)
    reason = "translation_failed"
    code = None
    if "insufficient_quota" in msg or "quota" in msg:
        reason = "openai_quota"
        code = "insufficient_quota"
    elif "timeout" in msg:
        reason = "timeout"
    elif "Unauthorized" in msg or "401" in msg:
        reason = "auth_error"
    return {
        "fail_open": True,
        "reason": reason,
        "code": code,
        "provider": "openai",
        "message": msg[:120],
    }


@app.websocket("/ws/stt/deepgram")
async def ws_stt_deepgram(websocket: WebSocket):
    src_lang_full = _clean_lang(websocket.query_params.get("source"), "ko")
    tgt_lang_full = _clean_lang(websocket.query_params.get("target"), "en")
    src_lang = _normalize_lang(src_lang_full, "ko")
    tgt_lang = _normalize_lang(tgt_lang_full, "en")
    ctx = _context_from_query_params(websocket.query_params)
    org_id, room_id = _resolve_room_context(
        org_id=ctx.get("orgId"),
        room_id=ctx.get("roomId"),
        service_key=ctx.get("serviceKey"),
        church_slug=ctx.get("churchSlug"),
    )
    service_key = ctx.get("serviceKey")
    church_slug = ctx.get("churchSlug")
    host_uid_claim = _uid_from_id_token(ctx.get("idToken")) or ctx.get("hostUid")
    host_token_claim = ctx.get("hostToken")
    early_commit = str(websocket.query_params.get("early") or websocket.query_params.get("early_commit") or "").lower() in {"1", "true", "yes", "on"}
    dg_language = _deepgram_language_preference(src_lang_full)
    dg_keywords = None if src_lang.startswith("ko") else []

    if org_id and not room_id:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "room_not_live"})
        await websocket.close(code=1008)
        return
    if org_id and not _can_host(org_id, host_uid=host_uid_claim, host_token=host_token_claim):
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "host_auth_failed"})
        await websocket.close(code=1008)
        return

    await websocket.accept()
    translation_ctx = TranslationContext()
    chunker = KoChunker() if early_commit and src_lang.startswith("ko") else None
    try:
        dg = await connect_to_deepgram(language=dg_language, keywords=dg_keywords)  # <-- dg is created here
    except Exception as e:
        await websocket.send_json({"type": "error", "message": f"Deepgram connect failed: {e}"})
        await websocket.close()
        return

    seq = 0
    closed = asyncio.Event()
    finalize_event = asyncio.Event()
    last_audio_touch_ts = 0.0
    prompt_overrides_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}

    def _cached_prompt_overrides(active_org_id: Optional[str]) -> tuple[Optional[str], Optional[str]]:
        clean_org_id = _clean_token(active_org_id)
        if not clean_org_id:
            return None, None
        cached = prompt_overrides_cache.get(clean_org_id)
        if cached is not None:
            return cached
        resolved = _prompt_overrides_for_org(clean_org_id)
        prompt_overrides_cache[clean_org_id] = resolved
        return resolved

    if org_id:
        prompt_overrides_cache[org_id] = _prompt_overrides_for_org(org_id)

    async def from_client_to_deepgram():
        nonlocal last_audio_touch_ts
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    try:
                        await dg.close()
                    except:
                        pass
                    break
                if (b := msg.get("bytes")):
                    # your AudioWorklet streams raw 16-bit PCM @ 48k
                    await dg.send(b)
                    if org_id and room_id:
                        now_ts = time.time()
                        if (now_ts - last_audio_touch_ts) >= 20:
                            last_audio_touch_ts = now_ts
                            try:
                                multichurch_store.touch_audio(org_id, room_id)
                            except Exception:
                                pass
                elif (t := msg.get("text")):
                    # allow client-side finalize
                    try:
                        payload = json.loads(t)
                        if payload.get("type") == "finalize":
                            finalize_event.set()
                            continue
                    except:
                        pass
        finally:
            closed.set()

    async def from_deepgram_to_server():
        """
        Option A: translate only when a sentence is complete.
        Commit rules:
          - speech_final=True  → commit immediately
          - or final text ends with sentence punctuation → commit
          - else start/refresh a ~1.2s timer; on timeout, commit whatever we have
        """
        SENTENCE_PUNCT = tuple(".?!。？！…")
        SENTENCE_PUNCT_CHARS = "".join(SENTENCE_PUNCT)
        COMMIT_WAIT_MS = 250
        CJK_PENDING_HOLD_MS = 600
        MIN_CONFIDENT_CHARS = 10
        KOREAN_SHORT_MIN_CHARS = 14
        KOREAN_EOS_RE = re.compile(
            r"(?:습니다|입니다|합니다|했습니다|할까요|했어요|했지요|했네요|예요|이에요|에요|일까요|였어요|였습니까|입니까|됩니까|나요|군요|지요|래요|랍니다|라네요|다|아요|어요|에요)$"
        )

        pending_src: str | None = None
        pending_speech_final = False
        pending_task: asyncio.Task | None = None
        last_preview_norm: str = ""

        def ends_like_sentence(t: str) -> bool:
            t = (t or "").rstrip()
            if not t:
                return False
            if src_lang.startswith("ko"):
                stripped = t.rstrip(SENTENCE_PUNCT_CHARS)
                if not stripped:
                    return False
                return bool(KOREAN_EOS_RE.search(stripped))
            last_char = t[-1]
            if last_char in SENTENCE_PUNCT:
                return True
            return False

        def norm_ws(s: str) -> str:
            return " ".join((s or "").split())

        def is_short_korean_clause(text: str) -> bool:
            if not text or not src_lang.startswith("ko"):
                return False
            clean = norm_ws(text)
            return len(clean) < KOREAN_SHORT_MIN_CHARS

        def looks_complete(text: str) -> bool:
            clean = norm_ws(text)
            if not clean:
                return False
            if ends_like_sentence(clean):
                return True
            if src_lang.startswith(CJK_NO_SPACE_PREFIXES):
                return False
            return len(clean) >= MIN_CONFIDENT_CHARS

        def should_apply_cjk_hold(text: str) -> bool:
            if not text:
                return False
            if not src_lang.startswith(CJK_NO_SPACE_PREFIXES):
                return False
            return not ends_like_sentence(text)

        def should_hold_short_korean(text: str, speech_final_flag: bool) -> bool:
            if speech_final_flag:
                return False
            return is_short_korean_clause(text)

        SUBSET_SUPPRESS_WINDOW_SEC = 4.0
        MIN_SUBSET_DELTA = 6
        CJK_NO_SPACE_PREFIXES = ("ko", "zh", "ja")

        async def send_translation(
            src_text_raw: str,
            *,
            partial: bool,
            live_mode_hint: str | None = None,
            meta_extra: dict[str, Any] | None = None,
            update_ctx: bool = True,
        ) -> None:
            nonlocal seq

            clean_src = norm_ws(src_text_raw)
            if not clean_src:
                return

            seq += 1
            live_mode = live_mode_hint or ("realtime" if partial else "live")
            meta_payload: dict[str, Any] = {
                "mode": "realtime" if partial else "realtime",
                "partial": partial,
                "segment_id": seq,
                "rev": 0,
                "seq": seq,
                "is_final": not partial,
            }
            if meta_extra:
                meta_payload.update(meta_extra)
            if org_id:
                meta_payload["org_id"] = org_id
            if room_id:
                meta_payload["room_id"] = room_id
            if service_key:
                meta_payload["service_key"] = service_key
            if church_slug:
                meta_payload["church_slug"] = church_slug

            translated = clean_src

            if not partial:
                script_match, match_score, script_version, script_threshold = script_store.match(
                    clean_src,
                    org_id=org_id,
                )
                scripture_hit = None
                if src_lang.startswith("ko"):
                    try:
                        scripture_hit = detect_scripture_verse(clean_src)
                    except Exception as exc:
                        print("[SCRIPTURE][error]", exc)

                if scripture_hit:
                    translated = scripture_hit.text
                    live_mode = "live"
                    meta_payload.update(
                        {
                            "kind": "scripture",
                            "reference": scripture_hit.reference,
                            "reference_ko": scripture_hit.source_reference,
                            "reference_en": scripture_hit.reference_en or scripture_hit.reference,
                            "version": scripture_hit.version,
                            "source_version": scripture_hit.source_version,
                            "book": scripture_hit.book,
                            "book_en": scripture_hit.book_en or scripture_hit.book,
                            "chapter": scripture_hit.chapter,
                            "verse": scripture_hit.verse,
                            "end_verse": scripture_hit.end_verse,
                            "source_text": scripture_hit.source_text,
                        }
                    )
                    print(f"[SCRIPTURE] matched {scripture_hit.reference}")
                elif script_match:
                    translated = script_match.target
                    live_mode = "pre"
                    meta_payload.update(
                        {
                            "mode": "pre",
                            "match_score": round(match_score, 4),
                            "matched_source": script_match.source,
                            "source_text": script_match.source,
                            "source_version": f"pre-script@{script_version}",
                            "threshold": script_threshold,
                        }
                    )
                    if update_ctx:
                        translation_ctx.last_english = translated
                elif src_lang == tgt_lang and src_lang_full == tgt_lang_full:
                    translated = clean_src
                else:
                    try:
                        custom_prompt, service_prompt = _cached_prompt_overrides(org_id)
                        translated = await translate_text(
                            clean_src,
                            src_lang_full,
                            tgt_lang_full,
                            ctx=translation_ctx,
                            update_ctx=update_ctx,
                            custom_prompt=custom_prompt,
                            service_prompt=service_prompt,
                        )
                        if update_ctx:
                            translation_ctx.last_english = translated
                    except Exception as e:
                        print("[TX] error:", e)
                        translated = clean_src
                        meta_payload.update(_fail_open_meta(e))
            else:
                # previews: skip scripture/script matching for speed
                if src_lang == tgt_lang and src_lang_full == tgt_lang_full:
                    translated = clean_src
                else:
                    try:
                        custom_prompt, service_prompt = _cached_prompt_overrides(org_id)
                        translated = await translate_text(
                            clean_src,
                            src_lang_full,
                            tgt_lang_full,
                            ctx=translation_ctx,
                            update_ctx=update_ctx,
                            custom_prompt=custom_prompt,
                            service_prompt=service_prompt,
                        )
                    except Exception as e:
                        print("[TX][preview] error:", e)
                        translated = clean_src
                        meta_payload.update(_fail_open_meta(e))
                if update_ctx:
                    translation_ctx.last_english = translated

            live_msg_new = {
                "mode": live_mode,
                "text": translated,
                "seq": seq,
                "src": {"text": clean_src, "lang": src_lang_full},
                "tgt": {"lang": tgt_lang_full},
                "meta": meta_payload.copy(),
            }
            if org_id:
                live_msg_new["orgId"] = org_id
            if room_id:
                live_msg_new["roomId"] = room_id
            live_msg_legacy = {
                "type": "translation",
                "payload": translated,
                "lang": tgt_lang_full,
                "meta": meta_payload.copy(),
            }

            try:
                await websocket.send_json(live_msg_new)
                await websocket.send_json(live_msg_legacy)
            except Exception as e:
                print("[DG] send back to producer failed:", e)

            try:
                if org_id and room_id:
                    await manager.broadcast_room(org_id, room_id, live_msg_new)
                    await manager.broadcast_room(org_id, room_id, live_msg_legacy)
                else:
                    await manager.broadcast(live_msg_new)
                    await manager.broadcast(live_msg_legacy)
                print(f"[BROADCAST] seq={seq} '{translated[:60]}'")
            except Exception as e:
                print("[DG] broadcast error:", e)

        async def emit_preview(src_text_raw: str):
            nonlocal last_preview_norm
            if not early_commit:
                return
            clean = norm_ws(src_text_raw)
            if not clean:
                return
            if clean == last_preview_norm:
                return
            last_preview_norm = clean
            await send_translation(src_text_raw, partial=True, live_mode_hint="realtime", update_ctx=False)

        async def commit_now(src_text_raw: str):
            nonlocal pending_src, pending_task, pending_speech_final, last_preview_norm
            if not src_text_raw or not src_text_raw.strip():
                return

            normalized = norm_ws(src_text_raw)
            if not normalized:
                return

            last_norm = getattr(commit_now, "_last_norm", "")
            last_ts = getattr(commit_now, "_last_commit_ts", 0.0)
            if normalized == last_norm:
                return

            if last_norm and len(normalized) < len(last_norm):
                delta = len(last_norm) - len(normalized)
                subset_lang = src_lang.startswith(CJK_NO_SPACE_PREFIXES)
                no_space = " " not in normalized and " " not in last_norm
                if subset_lang and no_space and delta >= MIN_SUBSET_DELTA:
                    is_edge_subset = last_norm.startswith(normalized) or last_norm.endswith(normalized)
                    recent_commit = (time.time() - last_ts) < SUBSET_SUPPRESS_WINDOW_SEC
                    if is_edge_subset and recent_commit:
                        print("[A][skip][subset]", normalized)
                        pending_src = None
                        pending_speech_final = False
                        if pending_task and not pending_task.done():
                            pending_task.cancel()
                        pending_task = None
                        return

            setattr(commit_now, "_last_norm", normalized)
            setattr(commit_now, "_last_commit_ts", time.time())
            setattr(commit_now, "_last_src", src_text_raw)

            print(f"[A] FINAL {src_lang_full}->{tgt_lang_full} src='{src_text_raw}'")
            await send_translation(src_text_raw, partial=False, live_mode_hint="live", update_ctx=True)

            last_preview_norm = ""
            pending_src = None
            pending_speech_final = False
            if pending_task and not pending_task.done():
                pending_task.cancel()
            pending_task = None

        async def arm_timer(wait_override_ms: int | None = None):
            nonlocal pending_task
            if pending_task and not pending_task.done():
                pending_task.cancel()

            snap = pending_src or ""
            wait_ms = wait_override_ms if wait_override_ms is not None else COMMIT_WAIT_MS

            async def _wait_and_commit(snap: str, delay_ms: int):
                try:
                    await asyncio.sleep(delay_ms / 1000.0)
                    if pending_src and norm_ws(pending_src) == norm_ws(snap):
                        await commit_now(pending_src)
                except asyncio.CancelledError:
                    pass

            pending_task = asyncio.create_task(_wait_and_commit(snap, wait_ms))

        async def flush_on_finalize():
            while True:
                try:
                    await finalize_event.wait()
                except asyncio.CancelledError:
                    break
                finalize_event.clear()

                if early_commit and chunker:
                    try:
                        now_ms = int(time.time() * 1000)
                        for piece in chunker.finalize(now_ms):
                            await emit_preview(piece)
                    except Exception as exc:
                        print("[EARLY][finalize][error]", exc)

                if pending_src:
                    try:
                        if should_hold_short_korean(pending_src, pending_speech_final):
                            await arm_timer(CJK_PENDING_HOLD_MS)
                        else:
                            await commit_now(pending_src)
                    except Exception:
                        pass

        finalize_task = asyncio.create_task(flush_on_finalize())

        try:
            async for raw in dg:  # <-- dg is in scope (captured from outer function)
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                if evt.get("type") != "Results":
                    continue

                ch = evt.get("channel") or {}
                alts = ch.get("alternatives") or []
                if not alts:
                    continue

                best = alts[0]
                transcript = (best.get("transcript") or "").strip()
                words = best.get("words") or []

                # Prefer word-level reconstruction to recover Korean spacing in partials/finals
                if src_lang.startswith("ko") and words:
                    joined_words = " ".join(
                        (w.get("punctuated_word") or w.get("word") or "").strip()
                        for w in words
                        if (w.get("word") or "").strip()
                    ).strip()
                    if joined_words:
                        if (" " not in transcript) or (len(joined_words) >= len(transcript) - 2):
                            transcript = joined_words

                is_final = bool(evt.get("is_final"))
                speech_final = bool(evt.get("speech_final") or False)

                # show partial text in the UI; optionally emit early preview translations
                if transcript and not is_final:
                    try:
                        await websocket.send_json({"type": "stt.partial", "text": transcript})
                    except:
                        pass

                    if early_commit:
                        now_ms = int(time.time() * 1000)
                        if chunker:
                            try:
                                for piece in chunker.push_partial(transcript, now_ms):
                                    await emit_preview(piece)
                            except Exception as exc:
                                print("[EARLY][chunker][error]", exc)
                        else:
                            # fallback: only emit when clause looks substantial
                            if ends_like_sentence(transcript) or len(norm_ws(transcript)) >= MIN_CONFIDENT_CHARS:
                                await emit_preview(transcript)

                    continue

                if not is_final:
                    continue

                if transcript:
                    pending_src = transcript
                    pending_speech_final = speech_final
                    if early_commit and chunker:
                        try:
                            now_ms = int(time.time() * 1000)
                            for piece in chunker.finalize(now_ms):
                                await emit_preview(piece)
                        except Exception as exc:
                            print("[EARLY][flush-finalize][error]", exc)

                print(f"[DG][A] final: speech_final={speech_final} src='{pending_src or ''}'")

                if speech_final and pending_src:
                    if looks_complete(pending_src):
                        await commit_now(pending_src)
                    else:
                        hold_ms = CJK_PENDING_HOLD_MS if should_apply_cjk_hold(pending_src) else None
                        await arm_timer(hold_ms)
                    continue

                if pending_src and ends_like_sentence(pending_src):
                    if should_hold_short_korean(pending_src, speech_final):
                        await arm_timer(CJK_PENDING_HOLD_MS)
                    else:
                        await commit_now(pending_src)
                    continue

                if pending_src:
                    hold_ms = CJK_PENDING_HOLD_MS if should_apply_cjk_hold(pending_src) else None
                    await arm_timer(hold_ms)

        finally:
            finalize_task.cancel()
            try:
                await finalize_task
            except asyncio.CancelledError:
                pass
            # best-effort flush on shutdown
            if pending_src:
                try:
                    await commit_now(pending_src)
                except Exception:
                    pass

    consumer = asyncio.create_task(from_client_to_deepgram())
    producer = asyncio.create_task(from_deepgram_to_server())
    await closed.wait()
    try:
        consumer.cancel()
        producer.cancel()
    except:
        pass

# ------------------------------------------------------------------------------
# Quick debug to prove the FE consumer is listening
# ------------------------------------------------------------------------------
@app.get("/debug/broadcast")
async def debug_broadcast(org_id: Optional[str] = Query(default=None), room_id: Optional[str] = Query(default=None)):
    msg_new = {"mode": "live", "text": "**TEST BROADCAST**", "seq": 999, "tgt": {"lang": "en"}}
    msg_legacy = {
        "type": "translation",
        "payload": "**TEST BROADCAST**",
        "lang": "en",
        "meta": {"mode": "realtime", "partial": False, "segment_id": 999, "rev": 0, "seq": 999},
    }
    clean_org = _clean_token(org_id)
    clean_room = _clean_token(room_id)
    if clean_org and clean_room:
        msg_new["orgId"] = clean_org
        msg_new["roomId"] = clean_room
        msg_legacy["orgId"] = clean_org
        msg_legacy["roomId"] = clean_room
        await manager.broadcast_room(clean_org, clean_room, msg_new)
        await manager.broadcast_room(clean_org, clean_room, msg_legacy)
        return {"ok": True, "scoped": True, "orgId": clean_org, "roomId": clean_room}
    await manager.broadcast(msg_new)
    await manager.broadcast(msg_legacy)
    return {"ok": True, "scoped": False}
DEFAULT_DG_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "ko")


def _deepgram_language_preference(raw: Optional[str]) -> str:
    """
    Map UI language codes to Deepgram's expected identifiers.
    Falls back to the configured default if nothing matches.
    """
    if not raw:
        return DEFAULT_DG_LANGUAGE
    token = raw.strip().lower()
    if not token:
        return DEFAULT_DG_LANGUAGE

    overrides = {
        "en": "en",
        "en-us": "en",
        "en-gb": "en",
        "ko": "ko",
        "ko-kr": "ko",
        "es": "es",
        "es-es": "es",
        "zh": "zh",
        "zh-cn": "zh",
        "zh-hans": "zh",
        "zh-tw": "zh",
        "zh-hant": "zh",
    }
    if token in overrides:
        return overrides[token]

    primary = token.split("-")[0]
    if primary in overrides:
        return overrides[primary]

    return primary or DEFAULT_DG_LANGUAGE

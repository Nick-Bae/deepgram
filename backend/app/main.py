
# backend/app/main.py
import os, json, asyncio, logging, time, re, base64
from collections import deque
from threading import Lock
from typing import Optional, Any, Callable, Awaitable
from urllib.parse import urlsplit
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from starlette.websockets import WebSocketState
import websockets

load_dotenv()


def _env_int(name: str, default: int, *, min_value: int, max_value: int) -> int:
    raw = (os.getenv(name) or "").strip()
    if not raw:
        return default
    try:
        parsed = int(raw)
    except ValueError:
        return default
    return max(min_value, min(max_value, parsed))


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "y", "on"}

# --- local modules (single import each) ---
from app.socket_manager import manager
from app.deepgram_session import connect_to_deepgram, deepgram_model_for_language, _build_keyterm_list, _build_replace_list, DG_DEBUG
from app.services.script_store import script_store
from app.services.multichurch_store import multichurch_store
from app.utils.translate import (
    _preprocess_source_text,
    is_invalid_translation_output,
    translate_text,
    translate_text_streaming,
    TranslationContext,
)
from app.scripture import detect_scripture_verse
from app.routes import translate as translate_routes  # your existing REST routes
from app.routes import examples as examples_routes
from app.routes import script as script_routes
from app.routes import prompt as prompt_routes
from app.routes import stt_keyterms as stt_keyterms_routes
from app.routes import multichurch as multichurch_routes
from app.routes import auth as auth_routes
from app.routes import billing as billing_routes
from app.routes import admin as admin_routes
from app.routes import sermon_review as sermon_review_routes
from app.sermon_review.lookup import get_reviewed_matches, get_reviewed_text
from app.auth.firebase_auth import verify_id_token_value
from app.chunker.ko_chunker import KoChunker
from app.env import ENV
from app.utils.korean_segments import (
    ends_with_standalone_da,
    is_strongly_incomplete_korean_segment,
    join_korean_stt_segments,
)
from app.gemini_live import (
    GEMINI_INPUT_SAMPLE_RATE,
    GEMINI_LIVE_TRANSLATE_MODEL,
    GEMINI_OUTPUT_SAMPLE_RATE,
    Pcm16Downsampler48To16,
    PcmChunkBuffer,
    api_key as gemini_api_key,
    audio_message as gemini_audio_message,
    merge_transcript as merge_gemini_transcript,
    parse_server_content as parse_gemini_server_content,
    setup_message as gemini_setup_message,
    target_language_code as gemini_target_language_code,
    websocket_url as gemini_websocket_url,
)
from app.security_log import security_event, client_ip as _security_client_ip


import collections as _collections
_failed_segments: _collections.deque = _collections.deque(maxlen=50)


def _safe_append_segment(
    org_id: str,
    room_id: str,
    seq: int,
    korean_text: str,
    english_text: str,
    mode: str,
    match_score: Optional[float],
    timestamp: str,
) -> None:
    # Flush previously failed segments first (best-effort retry)
    while _failed_segments:
        args = _failed_segments[0]
        try:
            multichurch_store.append_translation_segment(*args)
            _failed_segments.popleft()
        except Exception:
            break  # Still failing — retry on next call

    try:
        multichurch_store.append_translation_segment(
            org_id, room_id, seq=seq, korean_text=korean_text,
            english_text=english_text, mode=mode,
            match_score=match_score, timestamp=timestamp,
        )
    except Exception as exc:
        print(f"[SEG] Failed to save segment org={org_id} room={room_id} seq={seq}: {exc}")
        _failed_segments.append(
            (org_id, room_id, seq, korean_text, english_text, mode, match_score, timestamp)
        )


# Global display pacing config (broadcast to display clients)
APP_DISPLAY_SPEED = {"speed": 1.0}
ROOM_IDLE_TIMEOUT_SEC = int(os.getenv("ROOM_IDLE_TIMEOUT_SEC", "900"))  # 15 min
ROOM_MAX_DURATION_SEC = int(os.getenv("ROOM_MAX_DURATION_SEC", "10800"))  # 3 hours
ROOM_SWEEPER_INTERVAL_SEC = int(os.getenv("ROOM_SWEEPER_INTERVAL_SEC", "60"))
ROOM_USAGE_TICK_SEC = int(os.getenv("ROOM_USAGE_TICK_SEC", "300"))  # 5 min
ROOM_HOST_PRESENCE_GRACE_SEC = _env_int("ROOM_HOST_PRESENCE_GRACE_SEC", 300, min_value=0, max_value=3600)
STT_NO_SPEECH_TIMEOUT_SEC = _env_int("STT_NO_SPEECH_TIMEOUT_SEC", 120, min_value=30, max_value=3600)
WS_TRANSLATION_LIMITS_ENABLED = not _env_bool("DISABLE_WS_TRANSLATION_LIMITS", False)
WS_TRANSLATION_LIMIT_WINDOW_SECONDS = _env_int("WS_TRANSLATION_LIMIT_WINDOW_SECONDS", 60, min_value=5, max_value=3600)
WS_TRANSLATION_GLOBAL_MAX_REQUESTS_PER_WINDOW = _env_int(
    "WS_TRANSLATION_GLOBAL_MAX_REQUESTS_PER_WINDOW",
    2400,
    min_value=1,
    max_value=1_000_000,
)
WS_TRANSLATION_GLOBAL_MAX_TOKENS_PER_WINDOW = _env_int(
    "WS_TRANSLATION_GLOBAL_MAX_TOKENS_PER_WINDOW",
    1_200_000,
    min_value=100,
    max_value=50_000_000,
)
WS_TRANSLATION_ORG_MAX_REQUESTS_PER_WINDOW = _env_int(
    "WS_TRANSLATION_ORG_MAX_REQUESTS_PER_WINDOW",
    300,
    min_value=1,
    max_value=500_000,
)
WS_TRANSLATION_ORG_MAX_TOKENS_PER_WINDOW = _env_int(
    "WS_TRANSLATION_ORG_MAX_TOKENS_PER_WINDOW",
    150_000,
    min_value=100,
    max_value=20_000_000,
)
WS_TRANSLATION_UID_MAX_REQUESTS_PER_WINDOW = _env_int(
    "WS_TRANSLATION_UID_MAX_REQUESTS_PER_WINDOW",
    180,
    min_value=1,
    max_value=500_000,
)
WS_TRANSLATION_UID_MAX_TOKENS_PER_WINDOW = _env_int(
    "WS_TRANSLATION_UID_MAX_TOKENS_PER_WINDOW",
    90_000,
    min_value=100,
    max_value=20_000_000,
)
WS_TRANSLATION_ANON_MAX_REQUESTS_PER_WINDOW = _env_int(
    "WS_TRANSLATION_ANON_MAX_REQUESTS_PER_WINDOW",
    60,
    min_value=1,
    max_value=500_000,
)
WS_TRANSLATION_ANON_MAX_TOKENS_PER_WINDOW = _env_int(
    "WS_TRANSLATION_ANON_MAX_TOKENS_PER_WINDOW",
    30_000,
    min_value=100,
    max_value=20_000_000,
)
WS_TRANSLATION_PROMPT_TOKEN_OVERHEAD = _env_int(
    "WS_TRANSLATION_PROMPT_TOKEN_OVERHEAD",
    220,
    min_value=0,
    max_value=10_000,
)
_tx_window_lock = Lock()
_tx_window_entries: dict[str, deque[dict[str, float]]] = {}

# 16-bit PCM @ 48 kHz: 48000 samples/s × 2 bytes/sample = 96000 bytes/s
_DEEPGRAM_BYTES_PER_SECOND = 96000
_OPENAI_REALTIME_TRANSLATE_BYTES_PER_SECOND = 96000
_GEMINI_LIVE_TRANSLATE_BYTES_PER_SECOND = 96000
OPENAI_REALTIME_TRANSLATE_MODEL = os.getenv("OPENAI_REALTIME_TRANSLATE_MODEL", "gpt-realtime-translate")
OPENAI_REALTIME_TRANSLATE_URL = (
    "wss://api.openai.com/v1/realtime/translations"
    f"?model={OPENAI_REALTIME_TRANSLATE_MODEL}"
)
_room_sweeper_task: asyncio.Task | None = None

# Per-IP concurrent connection limit for unauthenticated /ws/translate viewers.
# Prevents a single IP from exhausting the connection manager.
_WS_VIEWER_MAX_CONNS_PER_IP = _env_int("WS_VIEWER_MAX_CONNS_PER_IP", 20, min_value=1, max_value=500)
_ws_viewer_ip_conns: dict[str, int] = {}
_ws_viewer_ip_lock = Lock()


# Localhost-only defaults — used in local development when no env var is set.
# Raw IP addresses are intentionally excluded: browsers treat http://127.0.0.1
# as a distinct origin from http://localhost, and IP-based CORS origins are
# harder to reason about and unnecessary for dev workflows.
_DEFAULT_CORS_ALLOW_ORIGINS = (
    "http://localhost:3000",
    "http://localhost:5173",
)

# Cloud Run always sets K_SERVICE; use it to detect a production deployment.
_IS_PRODUCTION = bool(os.getenv("K_SERVICE") or (os.getenv("APP_ENV") or "").lower() == "production")


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part and part.strip()]


_IP_HOST_RE = re.compile(r"^(\d{1,3}\.){3}\d{1,3}(:\d+)?$|^\[?[0-9a-fA-F:]+\]?(:\d+)?$")


def _normalize_origin(raw_origin: str) -> Optional[str]:
    token = (raw_origin or "").strip()
    if not token:
        return None
    if token == "*":
        print("[CORS] ignoring wildcard origin '*'; configure explicit origins in CORS_ALLOW_ORIGINS")
        return None
    parsed = urlsplit(token)
    if parsed.scheme not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    if parsed.path not in {"", "/"}:
        return None
    if parsed.query or parsed.fragment:
        return None
    # Reject raw IP addresses (including loopback) as CORS origins in production.
    # In development the default list only uses "localhost", so this only blocks
    # someone explicitly adding an IP to CORS_ALLOW_ORIGINS in production.
    if _IS_PRODUCTION and _IP_HOST_RE.match(parsed.netloc):
        print(
            f"[CORS] ignoring IP-address origin '{token}' in production; "
            "configure a hostname in CORS_ALLOW_ORIGINS"
        )
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def _resolve_cors_allow_origins() -> list[str]:
    raw_csv = (os.getenv("CORS_ALLOW_ORIGINS") or os.getenv("CORS_ALLOWED_ORIGINS") or "").strip()
    origins_raw: list[str] = []
    if raw_csv:
        origins_raw.extend(_split_csv(raw_csv))
    for legacy_name in ("FRONTEND_ORIGIN", "FRONTEND_URL"):
        token = (os.getenv(legacy_name) or "").strip()
        if token:
            origins_raw.append(token)
    if not origins_raw:
        if _IS_PRODUCTION:
            print(
                "[CORS] CORS_ALLOW_ORIGINS is not configured in production; "
                "starting with an empty browser allow-list"
            )
            return []
        origins_raw.extend(_DEFAULT_CORS_ALLOW_ORIGINS)

    seen: set[str] = set()
    resolved: list[str] = []
    for candidate in origins_raw:
        normalized = _normalize_origin(candidate)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        resolved.append(normalized)
    if not resolved:
        if _IS_PRODUCTION:
            print(
                "[CORS] no valid production CORS origins configured; "
                "starting with an empty browser allow-list"
            )
            return []
        raise RuntimeError("No valid CORS origins configured. Set CORS_ALLOW_ORIGINS to explicit http(s) origins.")
    return resolved

# ------------------------------------------------------------------------------
# ONE app only
# ------------------------------------------------------------------------------
app = FastAPI(
    title="Real-Time Translation Backend",
    version="1.0.0",
    docs_url=None if _IS_PRODUCTION else "/docs",
    redoc_url=None if _IS_PRODUCTION else "/redoc",
    openapi_url=None if _IS_PRODUCTION else "/openapi.json",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_resolve_cors_allow_origins(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept", "Origin"],
)


@app.middleware("http")
async def add_security_headers(request, call_next):
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    response.headers.setdefault("Cache-Control", "no-store")
    return response


# Security events logged:
#   401 — authentication failure (bad/missing token)
#   403 — authorization failure (authenticated but no permission)
#   422 — validation failure (malformed request body / path param)
#   429 — rate limit hit
_SECURITY_LOG_STATUSES = {401, 403, 422, 429}


@app.middleware("http")
async def log_security_events(request, call_next):
    response = await call_next(request)
    if response.status_code in _SECURITY_LOG_STATUSES:
        security_event(
            "http_security_response",
            status=response.status_code,
            method=request.method,
            path=request.url.path,
            ip=_security_client_ip(request),
        )
    return response

# Keep your existing HTTP routes under /api
app.include_router(translate_routes.router, prefix="/api")
app.include_router(examples_routes.router, prefix="/api")
app.include_router(script_routes.router, prefix="/api")
app.include_router(prompt_routes.router, prefix="/api")
app.include_router(stt_keyterms_routes.router, prefix="/api")
app.include_router(multichurch_routes.router, prefix="/api")
app.include_router(auth_routes.router, prefix="/api")
app.include_router(billing_routes.router, prefix="/api")
app.include_router(admin_routes.router, prefix="/api")
app.include_router(sermon_review_routes.router, prefix="/api")

@app.get("/")
def root():
    return {"ok": True, "msg": "server is live"}


def _clean_token(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    txt = str(raw).strip()
    return txt or None


def _estimate_translation_tokens(source_text: str) -> int:
    clean = " ".join((source_text or "").split())
    if not clean:
        return 0
    ascii_chars = sum(1 for ch in clean if ord(ch) < 128)
    non_ascii_chars = max(0, len(clean) - ascii_chars)
    source_tokens = max(1, (ascii_chars // 4) + int(non_ascii_chars * 1.4))
    target_tokens = max(8, int(source_tokens * 1.2))
    estimated = source_tokens + target_tokens + WS_TRANSLATION_PROMPT_TOKEN_OVERHEAD
    return max(64, estimated)


def _tx_scope_limits(org_id: Optional[str], host_uid: Optional[str]) -> list[tuple[str, int, int]]:
    clean_org = _clean_token(org_id)
    clean_uid = _clean_token(host_uid)
    scopes: list[tuple[str, int, int]] = [
        (
            "__global__",
            WS_TRANSLATION_GLOBAL_MAX_REQUESTS_PER_WINDOW,
            WS_TRANSLATION_GLOBAL_MAX_TOKENS_PER_WINDOW,
        )
    ]
    if clean_org:
        scopes.append(
            (
                f"org:{clean_org}",
                WS_TRANSLATION_ORG_MAX_REQUESTS_PER_WINDOW,
                WS_TRANSLATION_ORG_MAX_TOKENS_PER_WINDOW,
            )
        )
    elif clean_uid:
        scopes.append(
            (
                f"uid:{clean_uid}",
                WS_TRANSLATION_UID_MAX_REQUESTS_PER_WINDOW,
                WS_TRANSLATION_UID_MAX_TOKENS_PER_WINDOW,
            )
        )
    else:
        scopes.append(
            (
                "__anon__",
                WS_TRANSLATION_ANON_MAX_REQUESTS_PER_WINDOW,
                WS_TRANSLATION_ANON_MAX_TOKENS_PER_WINDOW,
            )
        )
    return scopes


def _prune_tx_entries(entries: deque[dict[str, float]], now: float) -> None:
    cutoff = now - float(WS_TRANSLATION_LIMIT_WINDOW_SECONDS)
    while entries and float(entries[0].get("ts", 0.0)) <= cutoff:
        entries.popleft()


def _window_tokens(entries: deque[dict[str, float]]) -> int:
    total = 0
    for row in entries:
        total += int(row.get("tokens", 0.0) or 0.0)
    return max(0, total)


def _reserve_translation_budget(
    *,
    org_id: Optional[str],
    host_uid: Optional[str],
    source_text: str,
) -> tuple[list[tuple[str, dict[str, float]]] | None, dict[str, Any] | None]:
    if not WS_TRANSLATION_LIMITS_ENABLED:
        return [], None
    est_tokens = _estimate_translation_tokens(source_text)
    now = time.monotonic()
    scopes = _tx_scope_limits(org_id, host_uid)
    with _tx_window_lock:
        for scope_key, req_limit, token_limit in scopes:
            bucket = _tx_window_entries.setdefault(scope_key, deque())
            _prune_tx_entries(bucket, now)
            req_used = len(bucket)
            token_used = _window_tokens(bucket)
            if req_used >= req_limit:
                return None, {
                    "reason": "translation_rate_limited",
                    "kind": "requests",
                    "scope": scope_key,
                    "used": req_used,
                    "limit": req_limit,
                    "windowSeconds": WS_TRANSLATION_LIMIT_WINDOW_SECONDS,
                    "estimatedTokens": est_tokens,
                }
            if token_used + est_tokens > token_limit:
                return None, {
                    "reason": "translation_rate_limited",
                    "kind": "tokens",
                    "scope": scope_key,
                    "used": token_used,
                    "limit": token_limit,
                    "windowSeconds": WS_TRANSLATION_LIMIT_WINDOW_SECONDS,
                    "estimatedTokens": est_tokens,
                }
        reservations: list[tuple[str, dict[str, float]]] = []
        for scope_key, _, _ in scopes:
            bucket = _tx_window_entries.setdefault(scope_key, deque())
            entry = {"ts": now, "tokens": float(est_tokens)}
            bucket.append(entry)
            reservations.append((scope_key, entry))
        return reservations, None


def _settle_translation_budget(
    reservations: list[tuple[str, dict[str, float]]] | None,
    *,
    actual_tokens: int,
) -> None:
    if reservations is None:
        return
    normalized = max(0, int(actual_tokens or 0))
    if not reservations:
        return
    with _tx_window_lock:
        for _, entry in reservations:
            entry["tokens"] = float(normalized)


def _rate_limit_meta(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "fail_open": True,
        "reason": "translation_rate_limited",
        "provider": "openai",
        "limit_kind": payload.get("kind"),
        "limit_scope": payload.get("scope"),
        "limit_window_seconds": payload.get("windowSeconds"),
        "limit_used": payload.get("used"),
        "limit": payload.get("limit"),
        "estimated_tokens": payload.get("estimatedTokens"),
    }


async def _translate_text_guarded(
    source_text: str,
    source_lang: str,
    target_lang: str,
    *,
    org_id: Optional[str],
    host_uid: Optional[str],
    ctx: Optional[TranslationContext],
    update_ctx: bool = True,
    custom_prompt: Optional[str] = None,
    service_prompt: Optional[str] = None,
    compact_prompt: bool = False,
    script_examples: Optional[list] = None,
    script_glossary: Optional[list] = None,
    max_tokens: Optional[int] = None,
    out_usage: Optional[dict] = None,
    model_override: Optional[str] = None,
) -> tuple[str, Optional[dict[str, Any]]]:
    reservations, blocked = _reserve_translation_budget(org_id=org_id, host_uid=host_uid, source_text=source_text)
    if blocked is not None:
        print(
            "[TX][rate_limit] blocked",
            blocked.get("scope"),
            blocked.get("kind"),
            f"used={blocked.get('used')}",
            f"limit={blocked.get('limit')}",
        )
        return "", _rate_limit_meta(blocked)
    usage: dict[str, Any] = {}
    retry_usage: dict[str, Any] = {}
    try:
        translated = await translate_text(
            source_text,
            source_lang,
            target_lang,
            ctx=ctx,
            update_ctx=update_ctx,
            custom_prompt=custom_prompt,
            service_prompt=service_prompt,
            compact_prompt=compact_prompt,
            script_examples=script_examples,
            script_glossary=script_glossary,
            org_id=org_id,
            max_tokens=max_tokens,
            usage_out=usage,
            model_override=model_override,
        )
        source_primary = _normalize_lang(source_lang, "ko")
        target_primary = _normalize_lang(target_lang, "en")
        mismatch = (
            source_primary != target_primary
            and bool(usage.get("failOpen"))
            and str(usage.get("errorMessage") or "") == "target_language_mismatch"
        )
        if mismatch:
            print(
                "[TX][target-language-retry]",
                f"source={source_primary}",
                f"target={target_primary}",
                f"text={source_text[:120]!r}",
            )
            translated = await translate_text(
                source_text,
                source_lang,
                target_lang,
                ctx=ctx,
                update_ctx=update_ctx,
                custom_prompt=custom_prompt,
                service_prompt=service_prompt,
                compact_prompt=True,
                script_examples=script_examples,
                script_glossary=script_glossary,
                org_id=org_id,
                max_tokens=max_tokens,
                usage_out=retry_usage,
                model_override=model_override,
                strict_target_only=True,
            )
            for key in ("promptTokens", "completionTokens", "totalTokens"):
                usage[key] = int(usage.get(key) or 0) + int(retry_usage.get(key) or 0)
            usage.pop("failOpen", None)
            usage.pop("errorMessage", None)
            for key, value in retry_usage.items():
                if key not in {"promptTokens", "completionTokens", "totalTokens"}:
                    usage[key] = value
            usage["targetLanguageRetry"] = True
    finally:
        _settle_translation_budget(reservations, actual_tokens=int(usage.get("totalTokens") or 0))
    source_primary = _normalize_lang(source_lang, "ko")
    target_primary = _normalize_lang(target_lang, "en")
    if source_primary != target_primary and bool(usage.get("failOpen")):
        error_message = str(usage.get("errorMessage") or "translation_failed")
        return "", _fail_open_meta(RuntimeError(error_message))
    if is_invalid_translation_output(translated, source_lang, target_lang):
        return "", {
            "fail_open": True,
            "reason": "target_language_mismatch",
            "code": "target_language_mismatch",
            "provider": "translation_guardrail",
            "message": "Suppressed Korean text in English translation output.",
        }
    if out_usage is not None:
        out_usage.update(usage)
    return translated, None


async def _translate_streaming_guarded(
    source_text: str,
    source_lang: str,
    target_lang: str,
    *,
    org_id: Optional[str],
    host_uid: Optional[str],
    ctx: Optional[TranslationContext],
    update_ctx: bool = True,
    custom_prompt: Optional[str] = None,
    service_prompt: Optional[str] = None,
    script_examples: Optional[list] = None,
    script_glossary: Optional[list] = None,
    on_token: Callable[[str], Awaitable[None]],
) -> tuple[str, Optional[dict[str, Any]]]:
    """Budget-guarded streaming translation. Emits tokens only after validation."""
    reservations, blocked = _reserve_translation_budget(
        org_id=org_id, host_uid=host_uid, source_text=source_text
    )
    if blocked is not None:
        return "", _rate_limit_meta(blocked)
    usage: dict[str, Any] = {}
    assembled = ""
    try:
        async for token in translate_text_streaming(
            source_text,
            source_lang,
            target_lang,
            ctx=ctx,
            update_ctx=update_ctx,
            custom_prompt=custom_prompt,
            service_prompt=service_prompt,
            script_examples=script_examples,
            script_glossary=script_glossary,
            org_id=org_id,
            usage_out=usage,
        ):
            assembled += token
    finally:
        _settle_translation_budget(
            reservations, actual_tokens=int(usage.get("totalTokens") or 0)
        )
    if usage.get("failOpen"):
        first_error = str(usage.get("errorMessage") or "translation_failed")
        print(f"[TX][stream][retry] reason={first_error[:120]}")
        retry_text, retry_meta = await _translate_text_guarded(
            source_text,
            source_lang,
            target_lang,
            org_id=org_id,
            host_uid=host_uid,
            ctx=ctx,
            update_ctx=update_ctx,
            custom_prompt=custom_prompt,
            service_prompt=service_prompt,
            compact_prompt=False,
            script_examples=script_examples,
            script_glossary=script_glossary,
        )
        if retry_text:
            await on_token(retry_text)
            print("[TX][stream][retry-success]")
            return retry_text, None
        meta = retry_meta or _fail_open_meta(RuntimeError(first_error))
        meta["retry_attempted"] = True
        return "", meta
    # Use the post-processed text (unmasked glossary, guardrails applied) that
    # translate_text_streaming stores in usage_out after the loop completes.
    translated = usage.get("finalText") or assembled
    if translated:
        await on_token(translated)
    return translated, None


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


def _try_reload_sermon(
    org_id: str,
    *,
    room_id: Optional[str] = None,
    service_key: Optional[str] = None,
    service_date: Optional[str] = None,
) -> bool:
    """
    Load the correct sermon into script_store for this room.
    Keyed by room_id when provided (service-isolation).
    Falls back to org-latest for legacy orgs with no service-specific sermon.
    Returns True if pairs were loaded, False otherwise. Never raises.
    """
    try:
        # If room_id given, check if already loaded for this room
        if room_id:
            count, _, _ = script_store.stats(room_id=room_id)
            if count > 0:
                return True
        else:
            count, _, _ = script_store.stats(org_id=org_id)
            if count > 0:
                return True

        doc = None
        resolved_service_date = (service_date or "").strip() or None
        # 1. Try service-specific published sermon (preferred)
        if service_key:
            if not resolved_service_date:
                try:
                    service_rows = multichurch_store.list_services_by_org_id(org_id) or []
                    for row in service_rows:
                        if str(row.get("serviceKey") or "").strip() == service_key:
                            candidate = str(row.get("publishedSermonDate") or "").strip()
                            if candidate:
                                resolved_service_date = candidate
                            break
                except Exception:
                    resolved_service_date = None

            if resolved_service_date:
                doc = multichurch_store.get_published_sermon(org_id, service_key, resolved_service_date)

            if not doc and service_date and resolved_service_date != service_date:
                logger.warning(
                    "no published sermon for %s/%s/%s, falling back to org-latest",
                    org_id, service_key, service_date,
                )

        # 2. Fall back to org-latest (legacy orgs / single-service orgs)
        if not doc:
            doc = multichurch_store.get_latest_sermon_pairs(org_id)

        if not doc or not doc.get("pairs"):
            return False

        # Re-check after Firestore round-trip
        if room_id:
            count, _, _ = script_store.stats(room_id=room_id)
        else:
            count, _, _ = script_store.stats(org_id=org_id)
        if count > 0:
            return True

        script_store.load(
            doc["pairs"],
            doc.get("threshold"),
            room_id=room_id,
            org_id=org_id if not room_id else None,
        )
        logger.info(
            "sermon loaded for room %s (%d pairs, service=%s, date=%s)",
            room_id or org_id, len(doc["pairs"]), service_key or "n/a", resolved_service_date or service_date or "n/a",
        )
        return True
    except Exception:
        return False


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


_spend_alert_sent: dict[str, str] = {}  # key: "openai-YYYYMM" / "deepgram-YYYYMM" → "sent"
_SPEND_ALERT_CHECK_INTERVAL = 3600  # 1 hour
_spend_alert_last_check: float = 0.0


def _check_spend_alerts() -> None:
    global _spend_alert_last_check
    now = time.time()
    if now - _spend_alert_last_check < _SPEND_ALERT_CHECK_INTERVAL:
        return
    _spend_alert_last_check = now
    try:
        from app.services.email_service import send_spend_alert_email
        cfg = multichurch_store.get_platform_config()
        alert_email = str(cfg.get("spendAlertEmail") or "").strip()
        openai_threshold = float(cfg.get("spendAlertOpenaiThresholdUsd") or 0)
        deepgram_threshold = float(cfg.get("spendAlertDeepgramThresholdUsd") or 0)
        if not alert_email or (openai_threshold <= 0 and deepgram_threshold <= 0):
            return
        usage = multichurch_store.get_platform_usage_summary()
        period_key = usage.get("periodKey", "")
        openai_spend = float((usage.get("liveTranslation") or {}).get("estimatedUsd") or 0) + \
                       float((usage.get("sermonPrep") or {}).get("estimatedUsd") or 0)
        deepgram_spend = float((usage.get("deepgram") or {}).get("estimatedUsd") or 0)

        if openai_threshold > 0 and openai_spend >= openai_threshold:
            alert_key = f"openai-{period_key}"
            if _spend_alert_sent.get(alert_key) != "sent":
                send_spend_alert_email(
                    to=alert_email,
                    provider="OpenAI",
                    spend_usd=openai_spend,
                    threshold_usd=openai_threshold,
                    period_key=period_key,
                )
                _spend_alert_sent[alert_key] = "sent"
                print(f"[SPEND_ALERT] OpenAI alert sent spend={openai_spend:.4f} threshold={openai_threshold}")

        if deepgram_threshold > 0 and deepgram_spend >= deepgram_threshold:
            alert_key = f"deepgram-{period_key}"
            if _spend_alert_sent.get(alert_key) != "sent":
                send_spend_alert_email(
                    to=alert_email,
                    provider="Deepgram",
                    spend_usd=deepgram_spend,
                    threshold_usd=deepgram_threshold,
                    period_key=period_key,
                )
                _spend_alert_sent[alert_key] = "sent"
                print(f"[SPEND_ALERT] Deepgram alert sent spend={deepgram_spend:.4f} threshold={deepgram_threshold}")
    except Exception as exc:
        print(f"[SPEND_ALERT] check failed: {exc}")


async def _room_sweeper_loop() -> None:
    while True:
        try:
            await asyncio.sleep(max(15, ROOM_SWEEPER_INTERVAL_SEC))
            candidate_rooms = multichurch_store.stale_live_rooms(
                idle_seconds=max(60, ROOM_IDLE_TIMEOUT_SEC),
                max_duration_seconds=max(600, ROOM_MAX_DURATION_SEC),
            )
            candidate_rooms.extend(
                multichurch_store.enforce_live_usage_caps(
                    tick_seconds=max(60, ROOM_USAGE_TICK_SEC),
                )
            )
            seen_room_keys = {
                (_clean_token(room.get("orgId")) or "", _clean_token(room.get("roomId")) or "")
                for room in candidate_rooms
            }
            if ROOM_HOST_PRESENCE_GRACE_SEC > 0:
                live = await asyncio.get_running_loop().run_in_executor(
                    None, multichurch_store.live_rooms
                )
                for room in live:
                    org_id = _clean_token(room.get("orgId"))
                    room_id = _clean_token(room.get("roomId"))
                    if not org_id or not room_id:
                        continue
                    elapsed = manager.note_room_host_absence(org_id, room_id)
                    room_key = (org_id, room_id)
                    if room_key in seen_room_keys:
                        continue
                    if elapsed >= ROOM_HOST_PRESENCE_GRACE_SEC:
                        candidate_rooms.append({"orgId": org_id, "roomId": room_id, "reason": "host_absent"})
                        seen_room_keys.add(room_key)
            for room in candidate_rooms:
                org_id = _clean_token(room.get("orgId"))
                room_id = _clean_token(room.get("roomId"))
                reason = _clean_token(room.get("reason")) or "idle_timeout"
                if not org_id or not room_id:
                    continue
                try:
                    result = multichurch_store.end_room(org_id, room_id, reason=reason)
                    if result.get("alreadyEnded"):
                        manager.forget_room(org_id, room_id)
                        print(f"[ROOM_SWEEPER] room already ended — no double billing org={org_id} room={room_id}")
                        continue
                except ValueError:
                    continue
                except Exception as exc:
                    print(f"[ROOM_SWEEPER] end_room failed org={org_id} room={room_id} err={exc}")
                    continue
                manager.forget_room(org_id, room_id)
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
                            "message": (
                                "Trial minutes exhausted. Upgrade to continue."
                                if reason == "trial_expired"
                                else (
                                    "Monthly limit reached. Please contact your admin."
                                    if reason == "monthly_limit_reached"
                                    else ("Host disconnected. Broadcast ended." if reason == "host_absent" else None)
                                )
                            ),
                        },
                    )
                except Exception:
                    pass
            _check_spend_alerts()
        except asyncio.CancelledError:
            break
        except Exception as exc:
            print(f"[ROOM_SWEEPER] loop error: {exc}")


async def _cleanup_live_rooms_on_startup() -> None:
    """End all Firestore rooms still marked live from a previous server run."""
    try:
        stale = multichurch_store.stale_live_rooms(idle_seconds=0, max_duration_seconds=0)
        if not stale:
            return
        print(f"[STARTUP] Ending {len(stale)} stale live room(s) from previous run")
        for room in stale:
            org_id = room.get("orgId")
            room_id = room.get("roomId")
            if not org_id or not room_id:
                continue
            try:
                multichurch_store.end_room(org_id, room_id, reason="server_restart")
                print(f"[STARTUP] Ended stale room org={org_id} room={room_id}")
            except Exception as exc:
                print(f"[STARTUP] Could not end stale room org={org_id} room={room_id}: {exc}")
    except Exception as exc:
        print(f"[STARTUP] stale room cleanup failed: {exc}")


@app.on_event("startup")
async def _on_startup():
    global _room_sweeper_task
    print(f"[MULTICHURCH] store={type(multichurch_store).__name__}")
    asyncio.create_task(_cleanup_live_rooms_on_startup())
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
    # Parse query params before accepting — ws.query_params is available at HTTP
    # upgrade time, so we can validate and reject without establishing the connection.
    qctx = _context_from_query_params(ws.query_params)
    joined_org_id, joined_room_id = _resolve_room_context(
        org_id=qctx.get("orgId"),
        room_id=qctx.get("roomId"),
        service_key=qctx.get("serviceKey"),
        church_slug=qctx.get("churchSlug"),
    )

    # Require a valid org+room — prevents anonymous orphan connections that would
    # accumulate in the connection manager and enable DoS.
    if not joined_org_id or not joined_room_id:
        security_event("ws_auth_rejected", path="/ws/translate", ip=_security_client_ip(ws),
                       detail="missing_org_or_room")
        await ws.close(code=1008)
        return

    # Per-IP concurrent connection limit — prevents a single client from exhausting
    # the connection manager before authentication completes.
    _viewer_ip = _security_client_ip(ws)
    with _ws_viewer_ip_lock:
        _current = _ws_viewer_ip_conns.get(_viewer_ip, 0)
        if _current >= _WS_VIEWER_MAX_CONNS_PER_IP:
            security_event("ws_conn_limit", path="/ws/translate", ip=_viewer_ip,
                           detail=f"limit={_WS_VIEWER_MAX_CONNS_PER_IP}")
            await ws.close(code=1008)
            return
        _ws_viewer_ip_conns[_viewer_ip] = _current + 1

    await manager.connect(ws)
    display_config = {"type": "display_config", "speed": APP_DISPLAY_SPEED["speed"]}
    try:
        await ws.send_json(display_config)
    except Exception:
        pass
    translation_ctx = TranslationContext()
    seq = 0
    # Session-level script context cache (avoid recomputing on every utterance)
    _cached_script_version_producer: int = -1
    _cached_script_examples_producer: list = []
    _cached_script_glossary_producer: list = []
    # Only accept a UID that has been cryptographically verified via Firebase ID token.
    # Raw hostUid from query params is intentionally ignored — it can be trivially spoofed
    # by anyone who knows an admin's Firebase UID.
    host_uid_claim = _uid_from_id_token(qctx.get("idToken"))
    host_token_claim = qctx.get("hostToken")
    joined_service_key = qctx.get("serviceKey")
    joined_church_slug = qctx.get("churchSlug")
    prompt_overrides_cache: dict[str, tuple[Optional[str], Optional[str]]] = {}
    _last_display_config_ts: float = 0.0

    # Verify host credentials before granting the role — never trust the role param alone.
    claimed_role = (qctx.get("role") or "listener").strip().lower()
    if claimed_role == "host":
        host_authed = _can_host(joined_org_id, host_uid=host_uid_claim, host_token=host_token_claim)
        joined_role = "host" if host_authed else "listener"
    else:
        joined_role = claimed_role if claimed_role in {"listener", "viewer"} else "listener"
        host_authed = False

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
        # Role already verified above — no second auth check needed.
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
        nonlocal seq, joined_org_id, joined_room_id, joined_service_key, joined_church_slug, host_uid_claim, host_token_claim, host_authed, _cached_script_version_producer, _cached_script_examples_producer, _cached_script_glossary_producer
        src_text = (payload.get("text") or "").strip()
        if not src_text:
            return

        ws_role = manager.get_role(ws)
        _host_uid_unused, host_token, id_token = _host_claims_from_payload(payload, qctx)
        verified_uid = _uid_from_id_token(id_token)
        if verified_uid:
            host_uid_claim = verified_uid
        # Unverified hostUid from message payload is intentionally ignored —
        # only a Firebase-verified idToken may update the trusted UID.
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
        src_text = _preprocess_source_text(src_text, src_lang_full).strip()
        if not src_text:
            return

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

        # Refresh session-level script context cache when store version changes
        _sv_check = script_store.stats(room_id=target_room_id)[2]
        if _sv_check != _cached_script_version_producer:
            _cached_script_version_producer = _sv_check
            _cached_script_glossary_producer = script_store.get_keyword_glossary(room_id=target_room_id)

        # STT vocabulary correction using sermon vocab
        _vocab_set_producer = script_store.get_vocab_set(room_id=target_room_id)
        if _vocab_set_producer:
            from app.utils.translate import _stt_vocab_correct
            src_text = _stt_vocab_correct(src_text, _vocab_set_producer)

        script_match, match_score, script_version, script_threshold, _script_examples_producer = await asyncio.get_running_loop().run_in_executor(
            None, lambda: script_store.match_with_examples(src_text, room_id=target_room_id, target_lang=tgt_lang_full)
        )
        _cached_script_examples_producer = _script_examples_producer
        live_mode = "live"

        reviewed_text = await _reviewed_text_for_live(
            org_id=target_org_id,
            service_key=payload_service_key,
            source_text=src_text,
            source_lang=src_lang_full,
            target_lang=tgt_lang_full,
        )
        if reviewed_text:
            translated = reviewed_text
            live_mode = "reviewed"
            meta_payload.update(
                {
                    "mode": "reviewed",
                    "source_version": "sermon-review",
                }
            )
            translation_ctx.remember(src_text, translated)
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
            translation_ctx.remember(src_text, translated)
        elif src_lang == tgt_lang and src_lang_full == tgt_lang_full:
            translated = src_text
        else:
            try:
                custom_prompt, service_prompt = _cached_prompt_overrides(target_org_id)
                _tx_usage: dict[str, Any] = {}
                translated, limit_meta = await _translate_text_guarded(
                    src_text,
                    src_lang_full,
                    tgt_lang_full,
                    org_id=target_org_id,
                    host_uid=host_uid_claim,
                    ctx=translation_ctx,
                    update_ctx=True,
                    custom_prompt=custom_prompt,
                    service_prompt=service_prompt,
                    script_examples=_cached_script_examples_producer,
                    script_glossary=_cached_script_glossary_producer,
                    out_usage=_tx_usage,
                )
                if limit_meta:
                    meta_payload.update(limit_meta)
                if target_org_id and _tx_usage.get("totalTokens"):
                    _p = int(_tx_usage.get("promptTokens") or 0)
                    _c = int(_tx_usage.get("completionTokens") or 0)
                    _t = int(_tx_usage.get("totalTokens") or 0)
                    _cfg = multichurch_store.get_platform_config()
                    _usd = (_p * _cfg["liveTranslationInputCostPerMillion"] + _c * _cfg["liveTranslationOutputCostPerMillion"]) / 1_000_000
                    try:
                        multichurch_store.record_live_translation_usage(
                            org_id=target_org_id,
                            prompt_tokens=_p,
                            completion_tokens=_c,
                            total_tokens=_t,
                            estimated_usd=_usd,
                        )
                    except Exception:
                        pass
            except Exception as exc:
                print("[WS translate][producer_commit][error]", exc)
                translated = ""
                meta_payload.update(_fail_open_meta(exc))

        translated = _suppress_invalid_translation_output(
            translated,
            src_lang_full,
            tgt_lang_full,
            meta_payload,
        )
        if meta_payload.get("reason") == "target_language_mismatch":
            print(
                "[WS translate][suppressed-target]",
                f"seq={seq}",
                f"source={src_lang_full}",
                f"target={tgt_lang_full}",
                f"text={src_text[:160]!r}",
            )

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
                await asyncio.gather(
                    manager.broadcast_room(target_org_id, target_room_id, live_msg_new),
                    manager.broadcast_room(target_org_id, target_room_id, live_msg_legacy),
                )
            else:
                await asyncio.gather(
                    manager.broadcast(live_msg_new),
                    manager.broadcast(live_msg_legacy),
                )
        except Exception as exc:
            print("[WS translate][broadcast][error]", exc)

        try:
            await ws.send_json(live_msg_legacy)
        except Exception:
            pass

        if not is_partial and target_org_id and target_room_id and src_text and translated:
            import datetime as _dt
            _seg_ts = _dt.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
            _seg_score = meta_payload.get("match_score") if live_mode == "pre" else None
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _safe_append_segment(
                    target_org_id, target_room_id, seq, src_text, translated, live_mode,
                    _seg_score, _seg_ts,
                ),
            )

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
            if mtype_l == "ping":
                try:
                    await ws.send_json(
                        {
                            "type": "pong",
                            "clientTs": msg.get("clientTs"),
                            "serverTs": int(time.time() * 1000),
                        }
                    )
                except Exception:
                    pass
                continue
            if mtype_l == "pong":
                continue
            if mtype_l in {"consumer_join", "join"}:
                joined_service_key = _clean_token(msg.get("serviceKey") or msg.get("service_key")) or joined_service_key
                joined_church_slug = _clean_token(msg.get("churchSlug") or msg.get("church_slug") or msg.get("slug")) or joined_church_slug
                req_org_id = _clean_token(msg.get("orgId") or msg.get("org_id")) or joined_org_id
                req_room_id = _clean_token(msg.get("roomId") or msg.get("room_id")) or joined_room_id
                _host_uid_unused, host_token, id_token = _host_claims_from_payload(msg, qctx)
                verified_uid = _uid_from_id_token(id_token)
                if verified_uid:
                    host_uid_claim = verified_uid
                # Unverified hostUid from message payload is intentionally ignored
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
                # Rate-limit: at most 1 display_config update per second per host.
                _now = time.time()
                if _now - _last_display_config_ts < 1.0:
                    continue
                _last_display_config_ts = _now
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
        with _ws_viewer_ip_lock:
            _remaining = _ws_viewer_ip_conns.get(_viewer_ip, 1) - 1
            if _remaining <= 0:
                _ws_viewer_ip_conns.pop(_viewer_ip, None)
            else:
                _ws_viewer_ip_conns[_viewer_ip] = _remaining
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


def _suppress_invalid_translation_output(
    translated: str,
    source_lang: str,
    target_lang: str,
    meta_payload: dict[str, Any],
) -> str:
    if not is_invalid_translation_output(translated, source_lang, target_lang):
        return translated
    meta_payload.update(
        {
            "fail_open": True,
            "reason": "target_language_mismatch",
            "code": "target_language_mismatch",
            "provider": "translation_guardrail",
            "message": "Suppressed Korean text in English translation output.",
        }
    )
    return ""


def _reject_invalid_curated_translation(
    translated: Optional[str],
    source_lang: str,
    target_lang: str,
    *,
    source_kind: str,
    source_text: str,
) -> Optional[str]:
    clean = (translated or "").strip()
    if not clean:
        return None
    if not is_invalid_translation_output(clean, source_lang, target_lang):
        return clean
    print(
        "[TX][curated-target-rejected]",
        f"kind={source_kind}",
        f"source={source_lang}",
        f"target={target_lang}",
        f"text={source_text[:160]!r}",
        f"curated={clean[:160]!r}",
    )
    return None


async def _reviewed_text_for_live(
    *,
    org_id: Optional[str],
    service_key: Optional[str],
    source_text: str,
    source_lang: str,
    target_lang: str,
) -> Optional[str]:
    if not org_id or not service_key or not source_text:
        return None
    try:
        reviewed_text = await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: get_reviewed_text(
                store=multichurch_store,
                org_id=org_id,
                service_key=service_key,
                korean_text=source_text,
            ),
        )
    except Exception as exc:
        print("[SERMON_REVIEW][lookup-error]", exc)
        return None
    return _reject_invalid_curated_translation(
        reviewed_text,
        source_lang,
        target_lang,
        source_kind="sermon-review",
        source_text=source_text,
    )


def _format_duration_label(seconds: int) -> str:
    total = max(1, int(seconds))
    minutes, remainder = divmod(total, 60)
    if minutes and remainder:
        minute_label = "minute" if minutes == 1 else "minutes"
        second_label = "second" if remainder == 1 else "seconds"
        return f"{minutes} {minute_label} {remainder} {second_label}"
    if minutes:
        minute_label = "minute" if minutes == 1 else "minutes"
        return f"{minutes} {minute_label}"
    second_label = "second" if total == 1 else "seconds"
    return f"{total} {second_label}"


def _stt_idle_timeout_message(timeout_seconds: int) -> str:
    return f"No speech detected for {_format_duration_label(timeout_seconds)}. Microphone stream stopped."


async def _stt_idle_watchdog(
    *,
    closed: asyncio.Event,
    last_speech_activity: Callable[[], float],
    timeout_seconds: int,
    websocket: WebSocket,
    deepgram: Any,
    org_id: Optional[str],
    room_id: Optional[str],
) -> None:
    while not closed.is_set():
        idle_for = time.monotonic() - last_speech_activity()
        remaining = timeout_seconds - idle_for
        if remaining <= 0:
            timeout_message = _stt_idle_timeout_message(timeout_seconds)
            print(f"[DG][idle-timeout] org={org_id} room={room_id} timeout={timeout_seconds}s")
            try:
                await websocket.send_json(
                    {
                        "type": "error",
                        "reason": "speech_idle_timeout",
                        "message": timeout_message,
                    }
                )
            except Exception:
                pass
            closed.set()
            try:
                await websocket.close(code=1000)
            except Exception:
                pass
            try:
                await deepgram.close()
            except Exception:
                pass
            break
        try:
            await asyncio.wait_for(closed.wait(), timeout=min(5.0, max(0.25, remaining)))
        except asyncio.TimeoutError:
            continue
        except asyncio.CancelledError:
            break


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
    # Raw hostUid from query params is not accepted — only a Firebase-verified idToken.
    host_uid_claim = _uid_from_id_token(ctx.get("idToken"))
    host_token_claim = ctx.get("hostToken")
    early_commit = str(websocket.query_params.get("early") or websocket.query_params.get("early_commit") or "").lower() in {"1", "true", "yes", "on"}
    dg_language = _deepgram_language_preference(src_lang_full)
    # dg_keywords is resolved after auth + websocket accept (needs org/room context)

    # Reject BEFORE accepting — prevents establishing Deepgram/OpenAI sessions
    # for unauthenticated callers and eliminates accept-then-close race.
    # org_id is always required; anonymous callers cannot use this endpoint.
    if not org_id or not _can_host(org_id, host_uid=host_uid_claim, host_token=host_token_claim):
        security_event("ws_auth_rejected", path="/ws/stt/deepgram", org_id=org_id or "",
                       ip=_security_client_ip(websocket), detail="host_auth_failed")
        await websocket.close(code=1008)
        return
    if not room_id:
        security_event("ws_auth_rejected", path="/ws/stt/deepgram", org_id=org_id,
                       ip=_security_client_ip(websocket), detail="missing_room_id")
        await websocket.close(code=1008)
        return

    await websocket.accept()
    translation_ctx = TranslationContext()
    # Session-level script context cache (avoid recomputing on every utterance)
    _cached_script_version_dg: int = -1
    _cached_script_glossary_dg: list = []

    # Auto-reload sermon from Firestore if script store is empty (fire-and-forget)
    if org_id:
        asyncio.get_running_loop().run_in_executor(
            None, lambda: _try_reload_sermon(
                org_id,
                room_id=room_id,
                service_key=service_key,
            )
        )

    chunker = KoChunker(
        waitk_lo=ENV.WAITK_LO,
        waitk_hi=ENV.WAITK_HI,
        silence_commit_ms=ENV.SILENCE_COMMIT_MS,
        max_precommit_tokens=ENV.MAX_PRECOMMIT_TOKENS,
    ) if early_commit and src_lang.startswith("ko") else None
    # Build merged keyterm list from all 3 tiers (Korean only)
    if src_lang.startswith("ko"):
        _org_keyterms = multichurch_store.get_org_stt_keyterms_for_session(org_id)
        # Tier 2a: glossary terms first (curated recurring sermon vocab), then broader set
        _glossary_ko  = [ko for ko, _ in script_store.get_keyword_glossary(room_id=room_id)] if room_id else []
        _vocab_rest   = list(script_store.get_vocab_set(room_id=room_id)) if room_id else []
        _sermon_vocab = _glossary_ko + [t for t in _vocab_rest if t not in set(_glossary_ko)]
        dg_keywords = _build_keyterm_list(
            org_custom=_org_keyterms,
            sermon_vocab=_sermon_vocab,
        )
        if DG_DEBUG:
            print(f"[DG] session keyterms: {len(dg_keywords)} total "
                  f"(org_custom={len(_org_keyterms)}, glossary={len(_glossary_ko)}, sermon={len(_sermon_vocab)})")
        # Replace: org corrections first, then built-in defaults
        _org_replacements = multichurch_store.get_org_stt_replacements_for_session(org_id)
        dg_replacements = _build_replace_list(org_custom=_org_replacements)
    else:
        dg_keywords = []
        dg_replacements = []

    try:
        dg = await connect_to_deepgram(model=deepgram_model_for_language(dg_language), language=dg_language, keywords=dg_keywords, replacements=dg_replacements)
    except Exception as e:
        await websocket.send_json({"type": "error", "message": f"Deepgram connect failed: {e}"})
        await websocket.close()
        return

    seq = 0
    closed = asyncio.Event()
    finalize_event = asyncio.Event()
    last_audio_touch_ts = 0.0
    last_speech_activity_ts = time.monotonic()
    total_audio_bytes = 0
    session_end_reason = "unknown"
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

    async def _send_to_producer(message: dict[str, Any]) -> bool:
        if closed.is_set():
            return False
        if websocket.application_state == WebSocketState.DISCONNECTED:
            return False
        if websocket.client_state == WebSocketState.DISCONNECTED:
            return False
        try:
            await websocket.send_json(message)
            return True
        except Exception:
            return False

    async def from_client_to_deepgram():
        nonlocal last_audio_touch_ts, total_audio_bytes, session_end_reason
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    session_end_reason = "browser_disconnect"
                    print(
                        f"[DG][session-end] reason=browser_disconnect "
                        f"org={org_id} room={room_id}"
                    )
                    try:
                        await dg.close()
                    except:
                        pass
                    break
                if (b := msg.get("bytes")):
                    # your AudioWorklet streams raw 16-bit PCM @ 48k
                    total_audio_bytes += len(b)
                    await dg.send(b)
                    if org_id and room_id:
                        now_ts = time.time()
                        if (now_ts - last_audio_touch_ts) >= 20:
                            last_audio_touch_ts = now_ts
                            asyncio.get_running_loop().run_in_executor(
                                None, multichurch_store.touch_audio, org_id, room_id
                            )
                elif (t := msg.get("text")):
                    # allow client-side finalize
                    try:
                        payload = json.loads(t)
                        if payload.get("type") == "finalize":
                            finalize_event.set()
                            continue
                    except:
                        pass
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session_end_reason = "audio_forward_error"
            print(
                f"[DG][session-end] reason=audio_forward_error "
                f"org={org_id} room={room_id} "
                f"error={type(exc).__name__}: {exc}"
            )
        finally:
            closed.set()

    async def from_deepgram_to_server():
        nonlocal last_audio_touch_ts, last_speech_activity_ts, session_end_reason
        """
        Option A: translate only when a sentence is complete.
        Commit rules:
          - speech_final=True  → commit immediately
          - or final text ends with sentence punctuation → commit
          - else start/refresh a ~1.2s timer; on timeout, commit whatever we have
        """
        SENTENCE_PUNCT = tuple(".?!。？！…")
        SENTENCE_PUNCT_CHARS = "".join(SENTENCE_PUNCT)
        COMMIT_WAIT_MS = ENV.COMMIT_WAIT_MS
        CJK_PENDING_HOLD_MS = ENV.CJK_PENDING_HOLD_MS
        CJK_UNPUNCTUATED_COMMIT_HOLD_MS = ENV.CJK_UNPUNCTUATED_COMMIT_HOLD_MS
        MIN_CONFIDENT_CHARS = 10
        KOREAN_SHORT_MIN_CHARS = 14
        KOREAN_EOS_RE = re.compile(
            r"(?:습니다|입니다|합니다|했습니다|할까요|했어요|했지요|했네요|예요|이에요|에요|일까요|였어요|였습니까|입니까|됩니까|나요|군요|지요|래요|랍니다|라네요|다|아요|어요|에요)$"
        )

        pending_src: str | None = None
        pending_speech_final = False
        pending_task: asyncio.Task | None = None
        emitted_review_segment_ids: set[str] = set()
        last_preview_norm: str = ""
        latest_partial: str = ""
        latest_partial_at: float = 0.0

        def ends_like_sentence(t: str) -> bool:
            t = (t or "").rstrip()
            if not t:
                return False
            if src_lang.startswith("ko"):
                stripped = t.rstrip(SENTENCE_PUNCT_CHARS)
                if not stripped:
                    return False
                if ends_with_standalone_da(stripped):
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

        def cjk_hold_ms(text: str) -> int:
            if src_lang.startswith("ko") and not ends_like_sentence(text):
                return max(CJK_PENDING_HOLD_MS, CJK_UNPUNCTUATED_COMMIT_HOLD_MS)
            return CJK_PENDING_HOLD_MS

        def should_hold_short_korean(text: str, speech_final_flag: bool) -> bool:
            if speech_final_flag:
                return False
            return is_short_korean_clause(text)

        SUBSET_SUPPRESS_WINDOW_SEC = 4.0
        MIN_SUBSET_DELTA = 6
        CJK_NO_SPACE_PREFIXES = ("ko", "zh", "ja")

        async def _reviewed_matches_for_source(src_text_raw: str) -> list[dict[str, Any]]:
            clean_live_src = norm_ws(_preprocess_source_text(src_text_raw, src_lang_full))
            if not clean_live_src or not org_id or not service_key:
                return []
            try:
                return await asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: get_reviewed_matches(
                        store=multichurch_store,
                        org_id=org_id,
                        service_key=service_key,
                        korean_text=clean_live_src,
                    ),
                )
            except Exception as exc:
                print("[SERMON_REVIEW][progress-error]", exc)
                return []

        async def emit_reviewed_segment_matches(src_text_raw: str) -> int:
            nonlocal seq
            matches = await _reviewed_matches_for_source(src_text_raw)
            if not matches:
                return 0

            emitted = 0
            for match in matches:
                segment_id = str(match.get("segmentId") or "").strip()
                if segment_id and segment_id in emitted_review_segment_ids:
                    continue
                source_text = norm_ws(
                    _preprocess_source_text(
                        str(match.get("original") or src_text_raw),
                        src_lang_full,
                    )
                )
                reviewed_text = _reject_invalid_curated_translation(
                    str(match.get("reviewedText") or ""),
                    src_lang_full,
                    tgt_lang_full,
                    source_kind="sermon-review",
                    source_text=source_text,
                )
                if not reviewed_text:
                    continue

                seq += 1
                assigned_seq = seq
                meta_payload: dict[str, Any] = {
                    "mode": "reviewed",
                    "partial": False,
                    "segment_id": assigned_seq,
                    "reviewed_segment_id": segment_id,
                    "rev": 0,
                    "seq": assigned_seq,
                    "is_final": True,
                    "source_text": source_text,
                    "source_version": "sermon-review",
                }
                if org_id:
                    meta_payload["org_id"] = org_id
                if room_id:
                    meta_payload["room_id"] = room_id
                if service_key:
                    meta_payload["service_key"] = service_key
                if church_slug:
                    meta_payload["church_slug"] = church_slug

                live_msg_new = {
                    "mode": "reviewed",
                    "text": reviewed_text,
                    "seq": assigned_seq,
                    "src": {"text": source_text, "lang": src_lang_full},
                    "tgt": {"lang": tgt_lang_full},
                    "meta": meta_payload.copy(),
                }
                if org_id:
                    live_msg_new["orgId"] = org_id
                if room_id:
                    live_msg_new["roomId"] = room_id
                live_msg_legacy = {
                    "type": "translation",
                    "payload": reviewed_text,
                    "lang": tgt_lang_full,
                    "meta": meta_payload.copy(),
                }

                try:
                    await _send_to_producer(live_msg_new)
                    await _send_to_producer(live_msg_legacy)
                    if org_id and room_id:
                        await asyncio.gather(
                            manager.broadcast_room(org_id, room_id, live_msg_new),
                            manager.broadcast_room(org_id, room_id, live_msg_legacy),
                        )
                    else:
                        await asyncio.gather(
                            manager.broadcast(live_msg_new),
                            manager.broadcast(live_msg_legacy),
                        )
                except Exception as exc:
                    print("[SERMON_REVIEW][progress-broadcast-error]", exc)
                    continue

                if segment_id:
                    emitted_review_segment_ids.add(segment_id)
                translation_ctx.remember(source_text, reviewed_text)
                emitted += 1
                print(
                    f"[SERMON_REVIEW][progress] service={service_key} "
                    f"segment={segment_id or '?'} seq={assigned_seq}"
                )

                if org_id and room_id:
                    import datetime as _dt

                    _seg_ts = (
                        _dt.datetime.utcnow().isoformat(timespec="milliseconds")
                        + "Z"
                    )
                    asyncio.get_running_loop().run_in_executor(
                        None,
                        lambda _org_id=org_id,
                        _room_id=room_id,
                        _seq=assigned_seq,
                        _source=source_text,
                        _reviewed=reviewed_text,
                        _ts=_seg_ts: _safe_append_segment(
                            _org_id,
                            _room_id,
                            _seq,
                            _source,
                            _reviewed,
                            "reviewed",
                            None,
                            _ts,
                        ),
                    )

            return emitted

        async def send_translation(
            src_text_raw: str,
            *,
            partial: bool,
            live_mode_hint: str | None = None,
            meta_extra: dict[str, Any] | None = None,
            update_ctx: bool = True,
        ) -> None:
            nonlocal seq, _cached_script_version_dg, _cached_script_glossary_dg

            clean_src = norm_ws(_preprocess_source_text(src_text_raw, src_lang_full))
            if not clean_src:
                return

            seq += 1
            assigned_seq = seq
            live_mode = live_mode_hint or ("realtime" if partial else "live")
            meta_payload: dict[str, Any] = {
                "mode": "realtime" if partial else "realtime",
                "partial": partial,
                "segment_id": assigned_seq,
                "rev": 0,
                "seq": assigned_seq,
                "is_final": not partial,
            }
            if meta_extra:
                meta_payload.update(meta_extra)
            meta_payload.setdefault("source_text", clean_src)
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
                # Refresh glossary cache when script store version changes
                _sv_dg = script_store.stats(room_id=room_id)[2]
                if _sv_dg != _cached_script_version_dg:
                    _cached_script_version_dg = _sv_dg
                    _cached_script_glossary_dg = script_store.get_keyword_glossary(room_id=room_id)

                # STT vocabulary correction using sermon vocab
                _vocab_set_dg = script_store.get_vocab_set(room_id=room_id)
                if _vocab_set_dg:
                    from app.utils.translate import _stt_vocab_correct
                    clean_src = _stt_vocab_correct(clean_src, _vocab_set_dg)

                _loop = asyncio.get_running_loop()
                script_match, match_score, script_version, script_threshold, _script_examples_dg = await _loop.run_in_executor(
                    None, lambda: script_store.match_with_examples(clean_src, room_id=room_id, target_lang=tgt_lang_full)
                )
                scripture_hit = None
                if src_lang.startswith("ko"):
                    try:
                        scripture_hit = await _loop.run_in_executor(
                            None, detect_scripture_verse, clean_src
                        )
                    except Exception as exc:
                        print("[SCRIPTURE][error]", exc)

                reviewed_text: Optional[str] = None
                if org_id and service_key:
                    try:
                        reviewed_text = await _loop.run_in_executor(
                            None,
                            lambda: get_reviewed_text(
                                store=multichurch_store,
                                org_id=org_id,
                                service_key=service_key,
                                korean_text=clean_src,
                            ),
                        )
                    except Exception as exc:
                        print("[SERMON_REVIEW][lookup-error]", exc)

                if reviewed_text:
                    reviewed_text = _reject_invalid_curated_translation(
                        reviewed_text,
                        src_lang_full,
                        tgt_lang_full,
                        source_kind="sermon-review",
                        source_text=clean_src,
                    )
                if reviewed_text:
                    translated = reviewed_text
                    live_mode = "reviewed"
                    meta_payload.update(
                        {
                            "mode": "reviewed",
                            "source_version": "sermon-review",
                        }
                    )
                    if update_ctx:
                        translation_ctx.remember(clean_src, translated)
                    print(
                        f"[SERMON_REVIEW][match] service={service_key} "
                        f"seq={assigned_seq}"
                    )
                elif scripture_hit:
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
                    if update_ctx:
                        translation_ctx.remember(clean_src, translated)
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
                        translation_ctx.remember(clean_src, translated)
                else:
                    if src_lang == tgt_lang and src_lang_full == tgt_lang_full:
                        translated = clean_src
                    else:
                        try:
                            custom_prompt, service_prompt = _cached_prompt_overrides(org_id)
                            _token_msg_base: dict[str, Any] = {
                                "type": "translation_stream_token",
                                "seq": assigned_seq,
                            }
                            if org_id:
                                _token_msg_base["orgId"] = org_id
                            if room_id:
                                _token_msg_base["roomId"] = room_id

                            async def _on_token(token: str) -> None:
                                msg = {**_token_msg_base, "token": token}
                                try:
                                    if org_id and room_id:
                                        await manager.broadcast_room(org_id, room_id, msg)
                                    else:
                                        await manager.broadcast(msg)
                                except Exception:
                                    pass

                            translated, limit_meta = await _translate_streaming_guarded(
                                clean_src,
                                src_lang_full,
                                tgt_lang_full,
                                org_id=org_id,
                                host_uid=host_uid_claim,
                                ctx=translation_ctx,
                                update_ctx=update_ctx,
                                custom_prompt=custom_prompt,
                                service_prompt=service_prompt,
                                script_examples=_script_examples_dg,
                                script_glossary=_cached_script_glossary_dg,
                                on_token=_on_token,
                            )
                            if limit_meta:
                                meta_payload.update(limit_meta)
                        except Exception as e:
                            print("[TX] error:", e)
                            translated = ""
                            meta_payload.update(_fail_open_meta(e))
            else:
                # previews: skip scripture/script matching for speed
                if src_lang == tgt_lang and src_lang_full == tgt_lang_full:
                    translated = clean_src
                else:
                    try:
                        custom_prompt, _service_prompt = _cached_prompt_overrides(org_id)
                        # Partial previews use a compact prompt (no Bible names block,
                        # no full service scripture) to cut ~1000-1500 prompt tokens and
                        # reduce time-to-first-token by ~300-600 ms.
                        translated, limit_meta = await _translate_text_guarded(
                            clean_src,
                            src_lang_full,
                            tgt_lang_full,
                            org_id=org_id,
                            host_uid=host_uid_claim,
                            ctx=translation_ctx,
                            update_ctx=update_ctx,
                            custom_prompt=custom_prompt,
                            service_prompt="",
                            compact_prompt=True,
                            max_tokens=120,
                            model_override=ENV.PARTIAL_TRANSLATION_MODEL,
                        )
                        if limit_meta:
                            meta_payload.update(limit_meta)
                    except Exception as e:
                        print("[TX][preview] error:", e)
                        translated = ""
                        meta_payload.update(_fail_open_meta(e))

            translated = _suppress_invalid_translation_output(
                translated,
                src_lang_full,
                tgt_lang_full,
                meta_payload,
            )

            live_msg_new = {
                "mode": live_mode,
                "text": translated,
                "seq": assigned_seq,
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
                sent_new = await _send_to_producer(live_msg_new)
                sent_legacy = await _send_to_producer(live_msg_legacy)
                if not sent_new or not sent_legacy:
                    print("[DG] producer socket already closed; skipped echo back to host")
            except Exception:
                pass

            try:
                if org_id and room_id:
                    await asyncio.gather(
                        manager.broadcast_room(org_id, room_id, live_msg_new),
                        manager.broadcast_room(org_id, room_id, live_msg_legacy),
                    )
                else:
                    await asyncio.gather(
                        manager.broadcast(live_msg_new),
                        manager.broadcast(live_msg_legacy),
                    )
                print(f"[BROADCAST] seq={assigned_seq} '{translated[:60]}'")
            except Exception as e:
                print("[DG] broadcast error:", e)

            if not partial and org_id and room_id and clean_src and translated:
                import datetime as _dt
                _seg_ts = _dt.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
                _seg_score = meta_payload.get("match_score") if live_mode == "pre" else None
                asyncio.get_running_loop().run_in_executor(
                    None,
                    lambda: _safe_append_segment(
                        org_id, room_id, assigned_seq, clean_src, translated, live_mode,
                        _seg_score, _seg_ts,
                    ),
                )

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
            nonlocal pending_src, pending_task, pending_speech_final, last_preview_norm, latest_partial, latest_partial_at
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

            reviewed_matches = await _reviewed_matches_for_source(src_text_raw)
            if reviewed_matches:
                emitted = await emit_reviewed_segment_matches(src_text_raw)
                print(
                    f"[SERMON_REVIEW][final-skip-combined] "
                    f"matches={len(reviewed_matches)} emitted={emitted}"
                )
            else:
                print(f"[A] FINAL {src_lang_full}->{tgt_lang_full} src='{src_text_raw}'")
                await send_translation(src_text_raw, partial=False, live_mode_hint="live", update_ctx=True)

            last_preview_norm = ""
            latest_partial = ""
            latest_partial_at = 0.0
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
                        if (
                            src_lang.startswith("ko")
                            and is_strongly_incomplete_korean_segment(pending_src)
                        ):
                            print("[A][hold][incomplete-ko]", pending_src)
                            return
                        await commit_now(pending_src)
                except asyncio.CancelledError:
                    pass

            pending_task = asyncio.create_task(_wait_and_commit(snap, wait_ms))

        async def flush_on_finalize():
            nonlocal latest_partial, latest_partial_at
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

                finalize_src = pending_src
                used_latest_partial_fallback = False
                if (
                    not finalize_src
                    and latest_partial
                    and (time.monotonic() - latest_partial_at) <= 2.0
                    and ends_like_sentence(latest_partial)
                ):
                    finalize_src = latest_partial
                    used_latest_partial_fallback = True
                    print("[A][finalize][latest-partial-fallback]", finalize_src)

                if finalize_src:
                    try:
                        if (
                            src_lang.startswith("ko")
                            and is_strongly_incomplete_korean_segment(finalize_src)
                        ):
                            print("[A][hold][finalize-incomplete-ko]", finalize_src)
                        elif (
                            not used_latest_partial_fallback
                            and should_hold_short_korean(finalize_src, pending_speech_final)
                        ):
                            await arm_timer(cjk_hold_ms(finalize_src))
                        else:
                            await commit_now(finalize_src)
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

                # Update idle clock only when Deepgram detects actual speech.
                if transcript:
                    last_speech_activity_ts = time.monotonic()

                # show partial text in the UI; optionally emit early preview translations
                if transcript and not is_final:
                    latest_partial = transcript
                    latest_partial_at = time.monotonic()
                    try:
                        await _send_to_producer({"type": "stt.partial", "text": transcript})
                    except:
                        pass

                    await emit_reviewed_segment_matches(transcript)

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
                    latest_partial = transcript
                    latest_partial_at = time.monotonic()
                    if (
                        pending_src
                        and src_lang.startswith("ko")
                        and is_strongly_incomplete_korean_segment(pending_src)
                    ):
                        transcript = join_korean_stt_segments(pending_src, transcript)
                        print(f"[A][join][incomplete-ko] src='{transcript}'")
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
                        hold_ms = cjk_hold_ms(pending_src) if should_apply_cjk_hold(pending_src) else None
                        await arm_timer(hold_ms)
                    continue

                if pending_src and ends_like_sentence(pending_src):
                    if should_hold_short_korean(pending_src, speech_final):
                        await arm_timer(cjk_hold_ms(pending_src))
                    else:
                        await commit_now(pending_src)
                    continue

                if pending_src:
                    hold_ms = cjk_hold_ms(pending_src) if should_apply_cjk_hold(pending_src) else None
                    await arm_timer(hold_ms)

            if not closed.is_set():
                session_end_reason = "deepgram_eof"
                print(
                    f"[DG][session-end] reason=deepgram_eof "
                    f"org={org_id} room={room_id} "
                    f"code={getattr(dg, 'close_code', None)} "
                    f"detail={getattr(dg, 'close_reason', None)}"
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not closed.is_set():
                session_end_reason = "deepgram_receive_error"
                print(
                    f"[DG][session-end] reason=deepgram_receive_error "
                    f"org={org_id} room={room_id} "
                    f"code={getattr(exc, 'code', None)} "
                    f"detail={getattr(exc, 'reason', None)} "
                    f"error={type(exc).__name__}: {exc}"
                )
        finally:
            closed.set()
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
    idle_watchdog = asyncio.create_task(
        _stt_idle_watchdog(
            closed=closed,
            last_speech_activity=lambda: last_speech_activity_ts,
            timeout_seconds=STT_NO_SPEECH_TIMEOUT_SEC,
            websocket=websocket,
            deepgram=dg,
            org_id=org_id,
            room_id=room_id,
        )
    )
    await closed.wait()
    consumer.cancel()
    producer.cancel()
    idle_watchdog.cancel()
    try:
        await asyncio.gather(consumer, producer, idle_watchdog, return_exceptions=True)
    except Exception:
        pass
    print(
        f"[DG][session-closed] reason={session_end_reason} "
        f"org={org_id} room={room_id} audio_bytes={total_audio_bytes}"
    )
    try:
        await dg.close()
    except Exception:
        pass
    if org_id and total_audio_bytes > 0:
        _audio_secs = total_audio_bytes / _DEEPGRAM_BYTES_PER_SECOND
        _cfg = multichurch_store.get_platform_config()
        _dg_usd = (_audio_secs / 60.0) * _cfg["deepgramCostPerMinute"]
        try:
            multichurch_store.record_deepgram_usage(
                org_id=org_id,
                audio_seconds=_audio_secs,
                estimated_usd=_dg_usd,
            )
        except Exception:
            pass

def _target_language_for_openai_realtime(raw: Optional[str]) -> str:
    token = (raw or "en").strip().lower()
    if not token:
        return "en"
    aliases = {
        "en-us": "en",
        "en-gb": "en",
        "ko-kr": "ko",
        "es-es": "es",
        "es-us": "es",
        "zh-cn": "zh",
        "zh-hans": "zh",
        "zh-tw": "zh",
        "zh-hant": "zh",
    }
    return aliases.get(token, token.split("-")[0] or "en")


def _downsample_pcm16_48k_to_24k(pcm: bytes) -> bytes:
    """Drop every other 16-bit sample. The browser worklet sends mono PCM16 at 48 kHz."""
    if len(pcm) < 4:
        return pcm
    even_len = len(pcm) - (len(pcm) % 4)
    out = bytearray(even_len // 2)
    j = 0
    for i in range(0, even_len, 4):
        out[j:j + 2] = pcm[i:i + 2]
        j += 2
    return bytes(out)


def _looks_like_english_sentence(text: str) -> bool:
    clean = (text or "").strip()
    if len(clean) < 8:
        return False
    return clean[-1:] in {".", "!", "?", "…"}


@app.websocket("/ws/stt/openai-realtime-translate")
async def ws_stt_openai_realtime_translate(websocket: WebSocket):
    src_lang_full = _clean_lang(websocket.query_params.get("source"), "ko")
    tgt_lang_full = _clean_lang(websocket.query_params.get("target"), "en")
    tgt_lang = _target_language_for_openai_realtime(tgt_lang_full)
    ctx = _context_from_query_params(websocket.query_params)
    org_id, room_id = _resolve_room_context(
        org_id=ctx.get("orgId"),
        room_id=ctx.get("roomId"),
        service_key=ctx.get("serviceKey"),
        church_slug=ctx.get("churchSlug"),
    )
    service_key = ctx.get("serviceKey")
    church_slug = ctx.get("churchSlug")
    host_uid_claim = _uid_from_id_token(ctx.get("idToken"))
    host_token_claim = ctx.get("hostToken")

    if not org_id or not _can_host(org_id, host_uid=host_uid_claim, host_token=host_token_claim):
        security_event("ws_auth_rejected", path="/ws/stt/openai-realtime-translate", org_id=org_id or "",
                       ip=_security_client_ip(websocket), detail="host_auth_failed")
        await websocket.close(code=1008)
        return
    if not room_id:
        security_event("ws_auth_rejected", path="/ws/stt/openai-realtime-translate", org_id=org_id,
                       ip=_security_client_ip(websocket), detail="missing_room_id")
        await websocket.close(code=1008)
        return

    api_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    if not api_key:
        await websocket.accept()
        await websocket.send_json({"type": "error", "message": "OPENAI_API_KEY is not configured"})
        await websocket.close(code=1011)
        return

    await websocket.accept()

    try:
        oai = await websockets.connect(
            OPENAI_REALTIME_TRANSLATE_URL,
            additional_headers={
                "Authorization": f"Bearer {api_key}",
                "OpenAI-Safety-Identifier": host_uid_claim or org_id or "worship-translation-host",
            },
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
        )
    except TypeError:
        # Compatibility with older websockets versions.
        oai = await websockets.connect(
            OPENAI_REALTIME_TRANSLATE_URL,
            extra_headers={
                "Authorization": f"Bearer {api_key}",
                "OpenAI-Safety-Identifier": host_uid_claim or org_id or "worship-translation-host",
            },
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=8 * 1024 * 1024,
        )
    except Exception as exc:
        await websocket.send_json({"type": "error", "message": f"OpenAI realtime translate connect failed: {exc}"})
        await websocket.close(code=1011)
        return

    await oai.send(json.dumps({
        "type": "session.update",
        "session": {
            "audio": {
                "input": {
                    "transcription": {"model": "gpt-realtime-whisper"},
                    "noise_reduction": {"type": "near_field"},
                },
                "output": {
                    "language": tgt_lang,
                },
            },
        },
    }))

    seq = 0
    closed = asyncio.Event()
    output_buffer = ""
    source_buffer = ""
    flush_task: asyncio.Task | None = None
    last_audio_touch_ts = 0.0
    total_audio_bytes = 0

    async def _send_to_producer(message: dict[str, Any]) -> None:
        if closed.is_set():
            return
        if websocket.application_state == WebSocketState.DISCONNECTED:
            return
        try:
            await websocket.send_json(message)
        except Exception:
            pass

    async def _broadcast_translation(text: str, *, partial: bool, src_text: str = "") -> None:
        nonlocal seq
        clean = " ".join((text or "").split())
        clean_src = " ".join((src_text or "").split())
        reviewed_text: Optional[str] = None
        if not partial and clean_src:
            reviewed_text = await _reviewed_text_for_live(
                org_id=org_id,
                service_key=service_key,
                source_text=clean_src,
                source_lang=src_lang_full,
                target_lang=tgt_lang_full,
            )
            if reviewed_text:
                clean = reviewed_text
        if not clean:
            return
        if not partial:
            seq += 1
        meta_payload: dict[str, Any] = {
            "mode": "openai_realtime_translate",
            "provider": "openai",
            "engine": "gpt-realtime-translate",
            "partial": partial,
            "segment_id": seq + (1 if partial else 0),
            "rev": 0,
            "seq": seq + (1 if partial else 0),
            "is_final": not partial,
        }
        if not partial and clean_src and clean == reviewed_text:
            meta_payload.update(
                {
                    "mode": "reviewed",
                    "source_version": "sermon-review",
                }
            )
        if org_id:
            meta_payload["org_id"] = org_id
        if room_id:
            meta_payload["room_id"] = room_id
        if service_key:
            meta_payload["service_key"] = service_key
        if church_slug:
            meta_payload["church_slug"] = church_slug

        live_msg_new = {
            "mode": "realtime" if partial else "live",
            "text": clean,
            "seq": meta_payload["seq"],
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
            "payload": clean,
            "lang": tgt_lang_full,
            "meta": meta_payload.copy(),
        }

        await _send_to_producer(live_msg_new)
        await _send_to_producer(live_msg_legacy)
        try:
            if org_id and room_id:
                await asyncio.gather(
                    manager.broadcast_room(org_id, room_id, live_msg_new),
                    manager.broadcast_room(org_id, room_id, live_msg_legacy),
                )
            else:
                await asyncio.gather(manager.broadcast(live_msg_new), manager.broadcast(live_msg_legacy))
        except Exception as exc:
            print("[OAI-RT][broadcast][error]", exc)

        if not partial and org_id and room_id and clean:
            if clean_src:
                try:
                    await manager.broadcast_room(org_id, room_id, {"type": "final_kr", "text": clean_src})
                except Exception:
                    pass
            import datetime as _dt
            _seg_ts = _dt.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _safe_append_segment(
                    org_id, room_id, seq, clean_src, clean, meta_payload["mode"], None, _seg_ts,
                ),
            )

    async def _commit_output(reason: str) -> None:
        nonlocal output_buffer, source_buffer, flush_task
        text = output_buffer.strip()
        if not text:
            return
        src_text = source_buffer.strip()
        output_buffer = ""
        source_buffer = ""
        flush_task = None
        print(f"[OAI-RT][commit][{reason}] '{text[:80]}'")
        await _broadcast_translation(text, partial=False, src_text=src_text)

    def _schedule_idle_flush(delay: float = 1.15) -> None:
        nonlocal flush_task
        if flush_task and not flush_task.done():
            flush_task.cancel()
        snapshot = output_buffer

        async def _flush_if_idle() -> None:
            try:
                await asyncio.sleep(delay)
                if output_buffer and output_buffer == snapshot:
                    await _commit_output("idle")
            except asyncio.CancelledError:
                pass

        flush_task = asyncio.create_task(_flush_if_idle())

    async def from_client_to_openai() -> None:
        nonlocal last_audio_touch_ts, total_audio_bytes
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if (b := msg.get("bytes")):
                    total_audio_bytes += len(b)
                    pcm24 = _downsample_pcm16_48k_to_24k(b)
                    await oai.send(json.dumps({
                        "type": "session.input_audio_buffer.append",
                        "audio": base64.b64encode(pcm24).decode("ascii"),
                    }))
                    if org_id and room_id:
                        now_ts = time.time()
                        if (now_ts - last_audio_touch_ts) >= 20:
                            last_audio_touch_ts = now_ts
                            asyncio.get_running_loop().run_in_executor(
                                None, multichurch_store.touch_audio, org_id, room_id
                            )
                elif (t := msg.get("text")):
                    try:
                        payload = json.loads(t)
                    except Exception:
                        payload = {}
                    if payload.get("type") in {"finalize", "stop"}:
                        await _commit_output("client_finalize")
                        if payload.get("type") == "stop":
                            break
        except Exception as exc:
            print("[OAI-RT][client->openai][error]", exc)
        finally:
            closed.set()

    async def from_openai_to_server() -> None:
        nonlocal output_buffer, source_buffer
        try:
            async for raw in oai:
                try:
                    event = json.loads(raw)
                except Exception:
                    continue
                etype = str(event.get("type") or "")
                if etype == "error":
                    err = event.get("error") or {}
                    msg = err.get("message") if isinstance(err, dict) else str(err)
                    await _send_to_producer({"type": "error", "message": msg or "OpenAI realtime translate error"})
                    continue
                if etype == "session.output_transcript.delta":
                    delta = str(event.get("delta") or "")
                    if not delta:
                        continue
                    output_buffer += delta
                    await _broadcast_translation(output_buffer, partial=True, src_text=source_buffer.strip())
                    if _looks_like_english_sentence(output_buffer):
                        await _commit_output("sentence")
                    else:
                        _schedule_idle_flush()
                    continue
                if etype == "session.input_transcript.delta":
                    delta = str(event.get("delta") or "")
                    if not delta:
                        continue
                    source_buffer += delta
                    await _send_to_producer({"type": "stt.partial", "text": source_buffer.strip()})
                    continue
                if etype.endswith(".done") or etype.endswith(".completed"):
                    if "output_transcript" in etype or etype == "session.output_audio.done":
                        await _commit_output("done")
        except Exception as exc:
            print("[OAI-RT][openai->server][error]", exc)
        finally:
            closed.set()

    consumer = asyncio.create_task(from_client_to_openai())
    producer = asyncio.create_task(from_openai_to_server())
    await closed.wait()
    consumer.cancel()
    producer.cancel()
    if flush_task and not flush_task.done():
        flush_task.cancel()
    try:
        await asyncio.gather(consumer, producer, return_exceptions=True)
    except Exception:
        pass
    try:
        await _commit_output("shutdown")
    except Exception:
        pass
    try:
        await oai.close()
    except Exception:
        pass
    if org_id and total_audio_bytes > 0:
        _audio_secs = total_audio_bytes / _OPENAI_REALTIME_TRANSLATE_BYTES_PER_SECOND
        _usd = (_audio_secs / 60.0) * 0.034
        print(f"[OAI-RT][usage] org={org_id} room={room_id} audio_secs={_audio_secs:.1f} estimated_usd={_usd:.4f}")


@app.websocket("/ws/stt/gemini-live-translate")
async def ws_stt_gemini_live_translate(websocket: WebSocket):
    src_lang_full = _clean_lang(websocket.query_params.get("source"), "ko")
    tgt_lang_full = _clean_lang(websocket.query_params.get("target"), "en")
    target_language = gemini_target_language_code(tgt_lang_full)
    ctx = _context_from_query_params(websocket.query_params)
    org_id, room_id = _resolve_room_context(
        org_id=ctx.get("orgId"),
        room_id=ctx.get("roomId"),
        service_key=ctx.get("serviceKey"),
        church_slug=ctx.get("churchSlug"),
    )
    service_key = ctx.get("serviceKey")
    church_slug = ctx.get("churchSlug")
    host_uid_claim = _uid_from_id_token(ctx.get("idToken"))
    host_token_claim = ctx.get("hostToken")

    if not org_id or not _can_host(org_id, host_uid=host_uid_claim, host_token=host_token_claim):
        security_event(
            "ws_auth_rejected",
            path="/ws/stt/gemini-live-translate",
            org_id=org_id or "",
            ip=_security_client_ip(websocket),
            detail="host_auth_failed",
        )
        await websocket.close(code=1008)
        return
    if not room_id:
        security_event(
            "ws_auth_rejected",
            path="/ws/stt/gemini-live-translate",
            org_id=org_id,
            ip=_security_client_ip(websocket),
            detail="missing_room_id",
        )
        await websocket.close(code=1008)
        return

    api_key = gemini_api_key()
    if not api_key:
        await websocket.accept()
        await websocket.send_json(
            {
                "type": "error",
                "message": "GEMINI_API_KEY or GOOGLE_API_KEY is not configured",
            }
        )
        await websocket.close(code=1011)
        return

    await websocket.accept()

    gemini = None
    try:
        gemini = await websockets.connect(
            gemini_websocket_url(api_key),
            ping_interval=20,
            ping_timeout=20,
            close_timeout=5,
            max_size=16 * 1024 * 1024,
        )
        await gemini.send(gemini_setup_message(target_language))

        setup_complete = False
        while not setup_complete:
            raw_setup = await asyncio.wait_for(gemini.recv(), timeout=20)
            try:
                setup_event = json.loads(raw_setup)
            except Exception:
                continue
            if setup_event.get("setupComplete") is not None:
                setup_complete = True
                break
            if setup_event.get("error"):
                raise RuntimeError(str(setup_event.get("error")))
    except Exception as exc:
        if gemini is not None:
            try:
                await gemini.close()
            except Exception:
                pass
        await websocket.send_json(
            {
                "type": "error",
                "message": f"Gemini Live Translate connect failed: {str(exc)[:240]}",
            }
        )
        await websocket.close(code=1011)
        return

    seq = 0
    closed = asyncio.Event()
    output_buffer = ""
    source_buffer = ""
    flush_task: asyncio.Task | None = None
    last_audio_touch_ts = 0.0
    total_audio_bytes = 0
    downsampler = Pcm16Downsampler48To16()
    chunk_buffer = PcmChunkBuffer()

    async def _send_to_producer(message: dict[str, Any]) -> None:
        if closed.is_set():
            return
        if websocket.application_state == WebSocketState.DISCONNECTED:
            return
        try:
            await websocket.send_json(message)
        except Exception:
            pass

    async def _broadcast_translation(text: str, *, partial: bool, src_text: str = "") -> None:
        nonlocal seq
        clean = " ".join((text or "").split())
        clean_src = " ".join((src_text or "").split())
        reviewed_text: Optional[str] = None
        if not partial and clean_src:
            reviewed_text = await _reviewed_text_for_live(
                org_id=org_id,
                service_key=service_key,
                source_text=clean_src,
                source_lang=src_lang_full,
                target_lang=tgt_lang_full,
            )
            if reviewed_text:
                clean = reviewed_text
        if not clean:
            return
        if not partial:
            seq += 1
        message_seq = seq + (1 if partial else 0)
        meta_payload: dict[str, Any] = {
            "mode": "gemini_live_translate",
            "provider": "google",
            "engine": GEMINI_LIVE_TRANSLATE_MODEL,
            "partial": partial,
            "segment_id": message_seq,
            "rev": 0,
            "seq": message_seq,
            "is_final": not partial,
        }
        if not partial and reviewed_text:
            meta_payload.update(
                {
                    "mode": "reviewed",
                    "source_version": "sermon-review",
                }
            )
        if org_id:
            meta_payload["org_id"] = org_id
        if room_id:
            meta_payload["room_id"] = room_id
        if service_key:
            meta_payload["service_key"] = service_key
        if church_slug:
            meta_payload["church_slug"] = church_slug

        live_msg_new = {
            "mode": "realtime" if partial else "live",
            "text": clean,
            "seq": message_seq,
            "src": {"text": clean_src, "lang": src_lang_full},
            "tgt": {"lang": tgt_lang_full},
            "meta": meta_payload.copy(),
            "orgId": org_id,
            "roomId": room_id,
        }
        live_msg_legacy = {
            "type": "translation",
            "payload": clean,
            "lang": tgt_lang_full,
            "meta": meta_payload.copy(),
        }

        await _send_to_producer(live_msg_new)
        await _send_to_producer(live_msg_legacy)
        try:
            await asyncio.gather(
                manager.broadcast_room(org_id, room_id, live_msg_new),
                manager.broadcast_room(org_id, room_id, live_msg_legacy),
            )
        except Exception as exc:
            print("[GEMINI-LIVE][broadcast][error]", exc)

        if not partial and clean_src:
            try:
                await manager.broadcast_room(
                    org_id,
                    room_id,
                    {"type": "final_kr", "text": clean_src},
                )
            except Exception:
                pass

        if not partial:
            import datetime as _dt

            segment_ts = _dt.datetime.utcnow().isoformat(timespec="milliseconds") + "Z"
            asyncio.get_running_loop().run_in_executor(
                None,
                lambda: _safe_append_segment(
                    org_id,
                    room_id,
                    seq,
                    clean_src,
                    clean,
                    meta_payload["mode"],
                    None,
                    segment_ts,
                ),
            )

    async def _broadcast_audio(data: str) -> None:
        if not data:
            return
        message = {
            "type": "translated_audio",
            "provider": "google",
            "engine": GEMINI_LIVE_TRANSLATE_MODEL,
            "encoding": "pcm_s16le",
            "sampleRate": GEMINI_OUTPUT_SAMPLE_RATE,
            "channels": 1,
            "data": data,
        }
        await _send_to_producer(message)
        try:
            await manager.broadcast_room(org_id, room_id, message)
        except Exception as exc:
            print("[GEMINI-LIVE][audio-broadcast][error]", exc)

    async def _commit_output(reason: str) -> None:
        nonlocal output_buffer, source_buffer, flush_task
        text = output_buffer.strip()
        if not text:
            return
        src_text = source_buffer.strip()
        output_buffer = ""
        source_buffer = ""
        flush_task = None
        print(f"[GEMINI-LIVE][commit][{reason}] '{text[:80]}'")
        await _broadcast_translation(text, partial=False, src_text=src_text)

    def _schedule_idle_flush(delay: float = 1.0) -> None:
        nonlocal flush_task
        if flush_task and not flush_task.done():
            flush_task.cancel()
        snapshot = output_buffer

        async def _flush_if_idle() -> None:
            try:
                await asyncio.sleep(delay)
                if output_buffer and output_buffer == snapshot:
                    await _commit_output("idle")
            except asyncio.CancelledError:
                pass

        flush_task = asyncio.create_task(_flush_if_idle())

    async def _send_pcm16(pcm16: bytes) -> None:
        if pcm16:
            await gemini.send(gemini_audio_message(pcm16))

    async def from_client_to_gemini() -> None:
        nonlocal last_audio_touch_ts, total_audio_bytes
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    break
                if (pcm48 := msg.get("bytes")):
                    total_audio_bytes += len(pcm48)
                    pcm16 = downsampler.push(pcm48)
                    for chunk in chunk_buffer.push(pcm16):
                        await _send_pcm16(chunk)
                    now_ts = time.time()
                    if (now_ts - last_audio_touch_ts) >= 20:
                        last_audio_touch_ts = now_ts
                        asyncio.get_running_loop().run_in_executor(
                            None,
                            multichurch_store.touch_audio,
                            org_id,
                            room_id,
                        )
                elif (text_msg := msg.get("text")):
                    try:
                        payload = json.loads(text_msg)
                    except Exception:
                        payload = {}
                    if payload.get("type") in {"finalize", "stop"}:
                        await _send_pcm16(chunk_buffer.flush())
                        if payload.get("type") == "stop":
                            break
        except Exception as exc:
            if not closed.is_set():
                print("[GEMINI-LIVE][client->gemini][error]", exc)
                await _send_to_producer(
                    {
                        "type": "error",
                        "message": f"Gemini audio stream failed: {str(exc)[:180]}",
                    }
                )
        finally:
            closed.set()

    async def from_gemini_to_server() -> None:
        nonlocal output_buffer, source_buffer
        try:
            async for raw in gemini:
                try:
                    event = json.loads(raw)
                except Exception:
                    continue

                if event.get("error"):
                    error = event.get("error")
                    message = error.get("message") if isinstance(error, dict) else str(error)
                    await _send_to_producer(
                        {
                            "type": "error",
                            "message": message or "Gemini Live Translate error",
                        }
                    )
                    break

                content = parse_gemini_server_content(event)
                if content.interrupted:
                    output_buffer = ""
                    source_buffer = ""
                    if flush_task and not flush_task.done():
                        flush_task.cancel()

                if content.input_transcript:
                    source_buffer = merge_gemini_transcript(
                        source_buffer,
                        content.input_transcript,
                    )
                    await _send_to_producer(
                        {"type": "stt.partial", "text": source_buffer.strip()}
                    )

                if content.output_transcript:
                    output_buffer = merge_gemini_transcript(
                        output_buffer,
                        content.output_transcript,
                    )
                    await _broadcast_translation(
                        output_buffer,
                        partial=True,
                        src_text=source_buffer,
                    )
                    _schedule_idle_flush()

                for audio_data in content.audio_chunks:
                    await _broadcast_audio(audio_data)

                if content.turn_complete:
                    await _commit_output("turn_complete")
        except Exception as exc:
            if not closed.is_set():
                print("[GEMINI-LIVE][gemini->server][error]", exc)
                await _send_to_producer(
                    {
                        "type": "error",
                        "message": f"Gemini Live Translate disconnected: {str(exc)[:180]}",
                    }
                )
        finally:
            closed.set()

    consumer = asyncio.create_task(from_client_to_gemini())
    producer = asyncio.create_task(from_gemini_to_server())
    await closed.wait()
    consumer.cancel()
    producer.cancel()
    if flush_task and not flush_task.done():
        flush_task.cancel()
    try:
        await asyncio.gather(consumer, producer, return_exceptions=True)
    except Exception:
        pass
    try:
        await _commit_output("shutdown")
    except Exception:
        pass
    if gemini is not None:
        try:
            await gemini.close()
        except Exception:
            pass
    if total_audio_bytes > 0:
        audio_secs = total_audio_bytes / _GEMINI_LIVE_TRANSLATE_BYTES_PER_SECOND
        print(
            f"[GEMINI-LIVE][usage] org={org_id} room={room_id} "
            f"audio_secs={audio_secs:.1f} input_rate={GEMINI_INPUT_SAMPLE_RATE}"
        )


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

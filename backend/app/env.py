from __future__ import annotations
import os
import uuid
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _env_str(*names: str, default: str = "") -> str:
    for name in names:
        raw = (os.getenv(name) or "").strip()
        if raw:
            return raw
    return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


class ENV:
    PORT: int = int(os.getenv("PORT", "8000"))
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    TRANSLATION_MODEL: str = _env_str("OPENAI_TRANSLATION_MODEL", "TRANSLATION_MODEL", "OPENAI_MODEL", default="gpt-4o")
    OPENAI_TRANSLATION_MODEL: str = TRANSLATION_MODEL
    SERMON_TRANSLATION_MODEL: str = _env_str("OPENAI_SERMON_TRANSLATION_MODEL", "OPENAI_SERMON_MODEL", default=TRANSLATION_MODEL)
    OPENAI_SERMON_TRANSLATION_MODEL: str = SERMON_TRANSLATION_MODEL
    CONTEXT_SUBJECT: str = os.getenv("CONTEXT_SUBJECT", "the congregation")
    CONTEXT_PRONOUN: str = os.getenv("CONTEXT_PRONOUN", "we")
    CONTEXT_MODE: str = os.getenv("CONTEXT_MODE", "corporate worship")
    PARTIAL_CADENCE_MS: int = int(os.getenv("PARTIAL_CADENCE_MS", "150"))
    SILENCE_COMMIT_MS: int = int(os.getenv("SILENCE_COMMIT_MS", "450"))
    COMMIT_WAIT_MS: int = int(os.getenv("COMMIT_WAIT_MS", "100"))
    CJK_PENDING_HOLD_MS: int = int(os.getenv("CJK_PENDING_HOLD_MS", "300"))
    CJK_UNPUNCTUATED_COMMIT_HOLD_MS: int = int(os.getenv("CJK_UNPUNCTUATED_COMMIT_HOLD_MS", "1200"))
    PARTIAL_TRANSLATION_MODEL: str = _env_str("OPENAI_PARTIAL_MODEL", default="gpt-4o-mini")
    MAX_PRECOMMIT_TOKENS: int = int(os.getenv("MAX_PRECOMMIT_TOKENS", "14"))
    WAITK_LO: int = int(os.getenv("WAITK_LO", "4"))
    WAITK_HI: int = int(os.getenv("WAITK_HI", "7"))

    # Sermon Review (editing-sermon feature) — Design §10.3
    SERMON_MAX_SEGMENTS: int = int(os.getenv("SERMON_MAX_SEGMENTS", "1000"))
    SERMON_MAX_UPLOAD_BYTES: int = int(
        os.getenv("SERMON_MAX_UPLOAD_BYTES", str(5 * 1024 * 1024))
    )
    SERMON_SOURCE_MAX_BYTES: int = int(
        os.getenv("SERMON_SOURCE_MAX_BYTES", str(1 * 1024 * 1024))
    )
    SERMON_XLSX_MAX_DECOMPRESSED_BYTES: int = int(
        os.getenv("SERMON_XLSX_MAX_DECOMPRESSED_BYTES", str(50 * 1024 * 1024))
    )

    # Redis Pub/Sub cross-instance fanout (redis-pubsub-fanout feature).
    # When enabled, broadcast_room publishes to Redis instead of only local sockets,
    # so listeners on any Cloud Run instance receive translations.
    REDIS_ENABLED: bool = _env_bool("REDIS_ENABLED", False)
    REDIS_HOST: str = _env_str("REDIS_HOST", default="127.0.0.1")
    REDIS_PORT: int = int(os.getenv("REDIS_PORT", "6379"))
    REDIS_PASSWORD: str = os.getenv("REDIS_PASSWORD", "")
    REDIS_CHANNEL_PREFIX: str = _env_str("REDIS_CHANNEL_PREFIX", default="worshiptranslate")
    REDIS_SEQ_TTL_SEC: int = int(os.getenv("REDIS_SEQ_TTL_SEC", "86400"))
    REDIS_CONNECT_TIMEOUT_SEC: float = float(os.getenv("REDIS_CONNECT_TIMEOUT_SEC", "5"))
    # Per-command timeout for SUBSCRIBE / UNSUBSCRIBE (they run under _lock, so
    # a hung Redis connection would otherwise stall all listener/host
    # registration on this instance).
    REDIS_COMMAND_TIMEOUT_SEC: float = float(os.getenv("REDIS_COMMAND_TIMEOUT_SEC", "5"))
    # In-process probe task cadence + per-tick deadline (PR #31 §3 W7).
    # Interval: how often the adapter publishes a probe marker on its
    # OWN probe channel and awaits its own subscriber. Deadline: how
    # long the probe task waits for the round-trip before emitting
    # `redis_probe_failed`. Interval bounds match PR #31 §3's stated
    # range; deadline bound conservatively avoids masking a stuck
    # subscriber behind a too-lenient window.
    REDIS_PROBE_INTERVAL_SEC: float = max(
        10.0, min(300.0, float(os.getenv("REDIS_PROBE_INTERVAL_SEC", "30")))
    )
    REDIS_PROBE_DEADLINE_SEC: float = max(
        0.5, min(10.0, float(os.getenv("REDIS_PROBE_DEADLINE_SEC", "2")))
    )
    INSTANCE_ID: str = _env_str("INSTANCE_ID", default=f"inst-{uuid.uuid4().hex[:12]}")

    @classmethod
    def resolve_translation_model(cls, model_override: str | None = None, *, sermon: bool = False) -> str:
        override = (model_override or "").strip()
        if override:
            return override
        return cls.SERMON_TRANSLATION_MODEL if sermon else cls.TRANSLATION_MODEL

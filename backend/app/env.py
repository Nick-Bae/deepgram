from __future__ import annotations
import os
from pathlib import Path

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env")


def _env_str(*names: str, default: str = "") -> str:
    for name in names:
        raw = (os.getenv(name) or "").strip()
        if raw:
            return raw
    return default


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

    @classmethod
    def resolve_translation_model(cls, model_override: str | None = None, *, sermon: bool = False) -> str:
        override = (model_override or "").strip()
        if override:
            return override
        return cls.SERMON_TRANSLATION_MODEL if sermon else cls.TRANSLATION_MODEL

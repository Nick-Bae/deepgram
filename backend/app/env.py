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
    TRANSLATION_MODEL: str = _env_str("OPENAI_TRANSLATION_MODEL", "TRANSLATION_MODEL", "OPENAI_MODEL", default="gpt-4o-mini")
    OPENAI_TRANSLATION_MODEL: str = TRANSLATION_MODEL
    SERMON_TRANSLATION_MODEL: str = _env_str("OPENAI_SERMON_TRANSLATION_MODEL", "OPENAI_SERMON_MODEL", default=TRANSLATION_MODEL)
    OPENAI_SERMON_TRANSLATION_MODEL: str = SERMON_TRANSLATION_MODEL
    CONTEXT_SUBJECT: str = os.getenv("CONTEXT_SUBJECT", "the congregation")
    CONTEXT_PRONOUN: str = os.getenv("CONTEXT_PRONOUN", "we")
    CONTEXT_MODE: str = os.getenv("CONTEXT_MODE", "corporate worship")
    PARTIAL_CADENCE_MS: int = int(os.getenv("PARTIAL_CADENCE_MS", "150"))
    SILENCE_COMMIT_MS: int = int(os.getenv("SILENCE_COMMIT_MS", "550"))
    MAX_PRECOMMIT_TOKENS: int = int(os.getenv("MAX_PRECOMMIT_TOKENS", "14"))
    WAITK_LO: int = int(os.getenv("WAITK_LO", "4"))
    WAITK_HI: int = int(os.getenv("WAITK_HI", "7"))

    @classmethod
    def resolve_translation_model(cls, model_override: str | None = None, *, sermon: bool = False) -> str:
        override = (model_override or "").strip()
        if override:
            return override
        return cls.SERMON_TRANSLATION_MODEL if sermon else cls.TRANSLATION_MODEL

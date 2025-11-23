from __future__ import annotations
import os

class ENV:
    PORT: int = int(os.getenv("PORT", "8000"))
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    TRANSLATION_MODEL: str = os.getenv("TRANSLATION_MODEL", "gpt-4o-mini")
    CONTEXT_SUBJECT: str = os.getenv("CONTEXT_SUBJECT", "the congregation")
    CONTEXT_PRONOUN: str = os.getenv("CONTEXT_PRONOUN", "we")
    CONTEXT_MODE: str = os.getenv("CONTEXT_MODE", "corporate worship")
    PARTIAL_CADENCE_MS: int = int(os.getenv("PARTIAL_CADENCE_MS", "150"))
    SILENCE_COMMIT_MS: int = int(os.getenv("SILENCE_COMMIT_MS", "550"))
    MAX_PRECOMMIT_TOKENS: int = int(os.getenv("MAX_PRECOMMIT_TOKENS", "14"))
    WAITK_LO: int = int(os.getenv("WAITK_LO", "4"))
    WAITK_HI: int = int(os.getenv("WAITK_HI", "7"))

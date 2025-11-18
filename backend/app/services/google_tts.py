"""Google Cloud Text-to-Speech helper."""
from __future__ import annotations

import asyncio
import os
from functools import lru_cache
from typing import Dict, Tuple

from google.cloud import texttospeech


LANGUAGE_FALLBACKS: Dict[str, Dict[str, str]] = {
    "en": {"language_code": "en-US", "name": "en-US-Neural2-F"},
    "ko": {"language_code": "ko-KR", "name": "ko-KR-Neural2-A"},
    "es": {"language_code": "es-US", "name": "es-US-Neural2-A"},
    "zh": {"language_code": "cmn-CN", "name": "cmn-CN-Wavenet-A"},
    "ja": {"language_code": "ja-JP", "name": "ja-JP-Neural2-D"},
}

DEFAULT_RATE = float(os.getenv("GOOGLE_TTS_SPEAKING_RATE", "1.0"))
DEFAULT_PITCH = float(os.getenv("GOOGLE_TTS_PITCH", "0.0"))


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _language_from_voice_name(name: str | None) -> str | None:
    if not name:
        return None
    parts = name.split("-")
    if len(parts) >= 2:
        return f"{parts[0]}-{parts[1]}"
    return None


def _normalize_language_code(raw: str | None) -> str:
    if not raw:
        return LANGUAGE_FALLBACKS["en"]["language_code"]
    lowered = raw.replace("_", "-").strip().lower()
    if not lowered:
        return LANGUAGE_FALLBACKS["en"]["language_code"]
    if "-" in lowered:
        first, second, *rest = lowered.split("-")
        second = second.upper() if second else "US"
        return f"{first.lower()}-{second}"
    base = lowered
    fallback = LANGUAGE_FALLBACKS.get(base)
    if fallback:
        return fallback["language_code"]
    # default to en-US but preserve provided base code if possible
    if len(base) == 2:
        return f"{base.lower()}-US"
    return LANGUAGE_FALLBACKS["en"]["language_code"]


def _voice_override_for(base: str) -> str | None:
    env_key = f"GOOGLE_TTS_VOICE_{base.upper()}"
    override = os.getenv(env_key)
    if override:
        stripped = override.strip()
        if stripped:
            return stripped
    return None


def _resolve_voice(language: str | None, explicit_voice: str | None) -> Tuple[str, str]:
    explicit_voice = (explicit_voice or "").strip()
    if explicit_voice:
        if ":" in explicit_voice:
            lang_part, voice_name = explicit_voice.split(":", 1)
            return _normalize_language_code(lang_part), voice_name.strip()
        inferred_lang = _language_from_voice_name(explicit_voice)
        lang_code = inferred_lang or _normalize_language_code(language)
        return lang_code, explicit_voice

    lang_code = _normalize_language_code(language)
    base = lang_code.split("-")[0]
    fallback = LANGUAGE_FALLBACKS.get(base, LANGUAGE_FALLBACKS["en"])
    voice_name = _voice_override_for(base) or os.getenv("GOOGLE_TTS_DEFAULT_VOICE", "").strip() or fallback["name"]
    return fallback["language_code"], voice_name


@lru_cache(maxsize=1)
def _client() -> texttospeech.TextToSpeechClient:
    return texttospeech.TextToSpeechClient()


async def synthesize_async(
    text: str,
    *,
    language: str | None = None,
    voice: str | None = None,
    speaking_rate: float | None = None,
    pitch: float | None = None,
) -> tuple[bytes, dict[str, str]]:
    if not text or not text.strip():
        raise ValueError("text is required for TTS")

    lang_code, voice_name = _resolve_voice(language, voice)
    rate = _clamp(speaking_rate if speaking_rate is not None else DEFAULT_RATE, 0.25, 4.0)
    pitch_val = _clamp(pitch if pitch is not None else DEFAULT_PITCH, -20.0, 20.0)

    def _synthesize_blocking() -> tuple[bytes, dict[str, str]]:
        client = _client()
        synthesis_input = texttospeech.SynthesisInput(text=text)
        voice_params = texttospeech.VoiceSelectionParams(
            language_code=lang_code,
            name=voice_name,
        )
        audio_config = texttospeech.AudioConfig(
            audio_encoding=texttospeech.AudioEncoding.MP3,
            speaking_rate=rate,
            pitch=pitch_val,
        )
        response = client.synthesize_speech(
            input=synthesis_input,
            voice=voice_params,
            audio_config=audio_config,
        )
        return response.audio_content, {"voice_name": voice_name, "language_code": lang_code}

    return await asyncio.to_thread(_synthesize_blocking)

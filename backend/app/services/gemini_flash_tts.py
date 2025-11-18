"""Gemini Flash TTS helper via Cloud Text-to-Speech."""
from __future__ import annotations

import asyncio
import base64
import os
from functools import lru_cache
from typing import Dict, Tuple

import httpx
import google.auth
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request

LANGUAGE_FALLBACKS: Dict[str, Dict[str, str]] = {
    "en": {"language_code": "en-US", "name": "en-US-Neural2-F"},
    "ko": {"language_code": "ko-KR", "name": "ko-KR-Neural2-A"},
    "es": {"language_code": "es-US", "name": "es-US-Neural2-A"},
    "zh": {"language_code": "cmn-CN", "name": "cmn-CN-Wavenet-A"},
    "ja": {"language_code": "ja-JP", "name": "ja-JP-Neural2-D"},
}

DEFAULT_RATE = float(os.getenv("GOOGLE_TTS_SPEAKING_RATE", "1.0"))
DEFAULT_PITCH = float(os.getenv("GOOGLE_TTS_PITCH", "0.0"))
DEFAULT_MODEL = os.getenv("GOOGLE_TTS_MODEL", "gemini-2.5-flash-tts").strip() or "gemini-2.5-flash-tts"
DEFAULT_ENDPOINT = os.getenv(
    "GOOGLE_TTS_ENDPOINT",
    "https://texttospeech.googleapis.com/v1/text:synthesize",
)
USER_PROJECT = os.getenv("GOOGLE_TTS_PROJECT", "").strip()
AUTH_SCOPES = ("https://www.googleapis.com/auth/cloud-platform",)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _normalize_language_code(raw: str | None) -> str:
    if not raw:
        return LANGUAGE_FALLBACKS["en"]["language_code"]
    lowered = raw.replace("_", "-").strip().lower()
    if not lowered:
        return LANGUAGE_FALLBACKS["en"]["language_code"]
    if "-" in lowered:
        first, second, *_ = lowered.split("-")
        second = second.upper() if second else "US"
        return f"{first.lower()}-{second}"
    base = lowered
    fallback = LANGUAGE_FALLBACKS.get(base)
    if fallback:
        return fallback["language_code"]
    if len(base) == 2:
        return f"{base.lower()}-US"
    return LANGUAGE_FALLBACKS["en"]["language_code"]


def _normalize_model_name(model: str | None) -> str:
    value = (model or DEFAULT_MODEL).strip()
    if not value:
        value = "gemini-2.5-flash-tts"
    if value.startswith("models/"):
        value = value.split("/", 1)[1]
    return value


def _resolve_endpoint(model_name: str) -> str:
    if "{model}" in DEFAULT_ENDPOINT:
        return DEFAULT_ENDPOINT.format(model=model_name)
    return DEFAULT_ENDPOINT


@lru_cache(maxsize=1)
def _credentials() -> Credentials:
    creds, _ = google.auth.default(scopes=AUTH_SCOPES)
    return creds


async def _access_token() -> str:
    creds = _credentials()

    def _refresh() -> str:
        if not creds.valid or creds.expired or creds.token is None:
            creds.refresh(Request())
        return creds.token or ""

    token = await asyncio.to_thread(_refresh)
    if not token:
        raise RuntimeError("Failed to obtain Google Cloud access token for Gemini TTS")
    return token


async def synthesize_async(
    text: str,
    *,
    language: str | None = None,
    voice: str | None = None,
    speaking_rate: float | None = None,
    pitch: float | None = None,
    model: str | None = None,
) -> tuple[bytes, dict[str, str]]:
    if not text or not text.strip():
        raise ValueError("text is required for TTS")

    lang_code = _normalize_language_code(language)
    rate = _clamp(speaking_rate if speaking_rate is not None else DEFAULT_RATE, 0.25, 4.0)
    pitch_val = _clamp(pitch if pitch is not None else DEFAULT_PITCH, -20.0, 20.0)
    model_name = _normalize_model_name(model)

    voice_payload: Dict[str, str] = {
        "languageCode": lang_code,
        "model_name": model_name,
    }
    if voice:
        voice_payload["name"] = voice.strip()

    audio_config = {
        "audioEncoding": "MP3",
        "speakingRate": rate,
        "pitch": pitch_val,
    }

    body = {
        "input": {"text": text},
        "voice": voice_payload,
        "audioConfig": audio_config,
    }

    token = await _access_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    if USER_PROJECT:
        headers["x-goog-user-project"] = USER_PROJECT

    endpoint = _resolve_endpoint(model_name)

    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(endpoint, json=body, headers=headers)
        if response.status_code >= 400:
            detail = ""
            err_reason = None
            try:
                data = response.json()
                err = data.get("error") or {}
                detail = err.get("message") or ""
                if isinstance(err, dict):
                    err_reason = err.get("status")
            except Exception:
                detail = response.text[:200]
            msg = detail or "Gemini TTS request failed"
            if err_reason:
                msg = f"{err_reason}: {msg}"
            raise RuntimeError(f"{msg} (HTTP {response.status_code})")
        payload = response.json()

    audio_b64 = payload.get("audioContent")
    if not audio_b64:
        raise RuntimeError("Gemini TTS response missing audioContent")

    audio_bytes = base64.b64decode(audio_b64)
    meta = {
        "voice_name": voice_payload.get("name") or "",
        "language_code": lang_code,
        "model": f"models/{model_name}",
    }
    return audio_bytes, meta

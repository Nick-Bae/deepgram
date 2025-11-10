# backend/app/deepgram_session.py
import os
from urllib.parse import urlencode
from typing import Optional, List, Tuple
from dotenv import load_dotenv
import websockets
from websockets import legacy as ws_legacy  # fallback for websockets<=13

load_dotenv()

def _int_env(name: str, default: int, *, min_value: Optional[int] = None, max_value: Optional[int] = None) -> int:
    raw = os.getenv(name)
    if not raw:
        return default
    token = raw.strip().split()[0]
    try:
        val = int(token)
    except ValueError:
        return default
    if min_value is not None and val < min_value:
        return default
    if max_value is not None and val > max_value:
        return default
    return val

DG_ENDPOINT = os.getenv("DEEPGRAM_ENDPOINT", "wss://api.deepgram.com/v1/listen")
DG_KEY      = os.getenv("DEEPGRAM_API_KEY")
DG_MODEL    = os.getenv("DEEPGRAM_MODEL", "nova-2")   # Korean supported
DG_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "ko")
DG_ENDPOINTING_MS = _int_env("DG_ENDPOINTING_MS", 3500, min_value=200, max_value=6000)
DG_UTTER_END_MS = _int_env("DG_UTTER_END_MS", 1800, min_value=500, max_value=6000)
ENV_KEYWORDS = [t.strip() for t in os.getenv("DEEPGRAM_KEYWORDS", "").split(",") if t.strip()]
DG_DEBUG    = os.getenv("DEEPGRAM_DEBUG", "0") not in ("0", "", "false", "False")

def _qs(
    model: str,
    language: str,
    sample_rate: int,
    keywords: Optional[List[str]],
    endpointing_ms: Optional[int],
    utter_end_ms: Optional[int],
) -> str:
    params: List[Tuple[str, str]] = [
        ("model", model),
        ("language", language),
        ("punctuate", "true"),
        ("smart_format", "true"),
        ("interim_results", "true"),
        ("encoding", "linear16"),
        ("sample_rate", str(sample_rate)),
        ("vad_events", "true"),
    ]
    if endpointing_ms and endpointing_ms > 0:
        params.append(("endpointing", str(endpointing_ms)))
    if utter_end_ms and utter_end_ms > 0:
        params.append(("utterance_end_ms", str(utter_end_ms)))

    # Repeated 'keywords' with a boost works on nova-2/enhanced/base
    if keywords and model in ("nova-2", "enhanced", "base"):
        for term in keywords:
            params.append(("keywords", f"{term}:3"))

    # If someone flips to nova-3 later, map keywords -> keyterm (nova-3 style)
    if model.startswith("nova-3") and keywords:
        for term in keywords:
            params.append(("keyterm", term))

    return urlencode(params, doseq=True)

async def connect_to_deepgram(
    model: Optional[str] = None,
    language: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    sample_rate: int = 48000,
):
    if not DG_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY not set")

    m  = model or DG_MODEL
    lg = language or DG_LANGUAGE
    kw = keywords if keywords is not None else ENV_KEYWORDS
    url = f"{DG_ENDPOINT}?{_qs(m, lg, sample_rate, kw, DG_ENDPOINTING_MS, DG_UTTER_END_MS)}"

    headers = {"Authorization": f"Token {DG_KEY}"}
    if DG_DEBUG:
        print(f"[DG] connecting: {url}")

    # websockets >=14: additional_headers
    try:
        return await websockets.connect(
            url,
            additional_headers=headers,
            ping_interval=20,
            open_timeout=20,
            max_size=None,
        )
    except TypeError:
        # websockets <=13: legacy API uses extra_headers
        return await ws_legacy.client.connect(
            url,
            extra_headers=headers,
            ping_interval=20,
            open_timeout=20,
            max_size=None,
        )

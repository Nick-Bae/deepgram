# backend/app/deepgram_session.py
import os
from urllib.parse import urlencode
from typing import Optional, List, Tuple
from dotenv import load_dotenv
import websockets
from websockets import legacy as ws_legacy  # fallback for websockets<=13

load_dotenv()

DG_ENDPOINT = os.getenv("DEEPGRAM_ENDPOINT", "wss://api.deepgram.com/v1/listen")
DG_KEY      = os.getenv("DEEPGRAM_API_KEY")
DG_MODEL    = os.getenv("DEEPGRAM_MODEL", "nova-2")   # Korean supported
DG_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "ko")
ENV_KEYWORDS = [t.strip() for t in os.getenv("DEEPGRAM_KEYWORDS", "").split(",") if t.strip()]
DG_DEBUG    = os.getenv("DEEPGRAM_DEBUG", "0") not in ("0", "", "false", "False")

def _qs(model: str, language: str, sample_rate: int, keywords: Optional[List[str]]) -> str:
    params: List[Tuple[str, str]] = [
        ("model", model),
        ("language", language),
        ("punctuate", "true"),
        ("smart_format", "true"),
        ("interim_results", "true"),
        ("encoding", "linear16"),
        ("sample_rate", str(sample_rate)),
        ("endpointing", "3500"),
        ("utterance_end_ms", "1800"),
        ("vad_events", "true"),
    ]

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
    url = f"{DG_ENDPOINT}?{_qs(m, lg, sample_rate, kw)}"

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

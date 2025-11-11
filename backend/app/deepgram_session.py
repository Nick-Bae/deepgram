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
DG_MODEL    = os.getenv("DEEPGRAM_MODEL", "nova-3")   # Korean supported
DG_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "ko")
DG_ENDPOINTING_MS = _int_env("DG_ENDPOINTING_MS", 3500, min_value=200, max_value=6000)
DG_UTTER_END_MS = _int_env("DG_UTTER_END_MS", 1800, min_value=500, max_value=6000)
ENV_KEYWORDS = [t.strip() for t in os.getenv("DEEPGRAM_KEYWORDS", "").split(",") if t.strip()]
DG_KEYWORDS_LIMIT = _int_env("DEEPGRAM_KEYWORDS_LIMIT", 60, min_value=0, max_value=200)
DG_DEBUG    = os.getenv("DEEPGRAM_DEBUG", "0") not in ("0", "", "false", "False")


def _normalize_keyword_entries(raw: Optional[List[str]]) -> List[Tuple[str, Optional[str]]]:
    """Split optional boost values (term:boost) and deduplicate terms."""
    if not raw:
        return []

    normalized: List[Tuple[str, Optional[str]]] = []
    seen: set[str] = set()

    for entry in raw:
        token = (entry or "").strip()
        if not token:
            continue

        term, _, boost = token.partition(":")
        term = term.strip()
        if not term:
            continue

        key = term.lower()
        if key in seen:
            continue
        seen.add(key)

        boost = boost.strip()
        if boost:
            try:
                float(boost)
            except ValueError:
                boost = ""
        normalized.append((term, boost or None))

    return normalized

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

    normalized_keywords = _normalize_keyword_entries(keywords)
    if DG_KEYWORDS_LIMIT and len(normalized_keywords) > DG_KEYWORDS_LIMIT:
        if DG_DEBUG:
            print(f"[DG] trimming keywords {len(normalized_keywords)} → {DG_KEYWORDS_LIMIT}")
        normalized_keywords = normalized_keywords[:DG_KEYWORDS_LIMIT]

    # Repeated 'keywords' with a boost works on nova-2/enhanced/base
    if normalized_keywords and model in ("nova-2", "enhanced", "base"):
        for term, boost in normalized_keywords:
            bias = boost or "3"
            params.append(("keywords", f"{term}:{bias}"))

    # If someone flips to nova-3 later, map keywords -> keyterm (nova-3 style)
    if model.startswith("nova-3") and normalized_keywords:
        for term, _ in normalized_keywords:
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

# backend/app/deepgram_session.py
import os
import re
from pathlib import Path
from urllib.parse import urlencode
from typing import Optional, List, Tuple
from dotenv import load_dotenv
import websockets
from websockets import legacy as ws_legacy  # fallback for websockets<=13
from websockets.exceptions import InvalidStatus
from websockets.legacy.exceptions import InvalidStatusCode

from app.config.deepgram_keywords import DEFAULT_DEEPGRAM_KEYWORDS, DEFAULT_DEEPGRAM_REPLACEMENTS

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
DG_MODEL_FALLBACK = os.getenv("DEEPGRAM_MODEL_FALLBACK", "nova-2")  # for languages nova-3 doesn't support
DG_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "ko")

# nova-3 does not support Chinese; use nova-2 fallback for these language codes.
_NOVA3_UNSUPPORTED = frozenset({"zh", "zh-cn", "zh-sg", "zh-tw", "zh-hk", "cmn", "yue"})
DG_ENDPOINTING_MS = _int_env("DG_ENDPOINTING_MS", 500, min_value=200, max_value=6000)
DG_UTTER_END_MS = _int_env("DG_UTTER_END_MS", 1000, min_value=1000, max_value=6000)
_ENV_KEYWORDS = [t.strip() for t in os.getenv("DEEPGRAM_KEYWORDS", "").split(",") if t.strip()]
DG_KEYWORDS_LIMIT = _int_env("DEEPGRAM_KEYWORDS_LIMIT", 100, min_value=0, max_value=200)
DG_DEBUG    = os.getenv("DEEPGRAM_DEBUG", "0") not in ("0", "", "false", "False")
DG_MAX_URL_CHARS = _int_env("DG_MAX_URL_CHARS", 7500, min_value=2000, max_value=16000)

_KEYWORD_FILE_ENV = os.getenv("DEEPGRAM_KEYWORDS_FILE")
DG_KEYWORDS_FILE = Path(_KEYWORD_FILE_ENV).expanduser() if _KEYWORD_FILE_ENV else None
_KEYWORD_CACHE: Optional[List[str]] = None
_KEYWORD_MTIME: Optional[float] = None
_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_MAX_TERM_CHARS = 100


def _inline_keywords() -> List[str]:
    return list(_ENV_KEYWORDS or DEFAULT_DEEPGRAM_KEYWORDS)


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

        term, sep, boost = token.partition(":")
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
                continue
        elif sep:
            continue
        normalized.append((term, boost or None))

    return normalized


def _clean_deepgram_text(value: object, *, allow_colon: bool = True) -> str:
    text = _CONTROL_CHARS_RE.sub(" ", str(value or "")).strip()
    text = re.sub(r"\s+", " ", text)
    if not allow_colon and ":" in text:
        return ""
    return text[:_MAX_TERM_CHARS].strip()


def _load_keywords_from_file() -> Optional[List[str]]:
    global _KEYWORD_CACHE, _KEYWORD_MTIME
    if DG_KEYWORDS_FILE is None:
        return None
    if not DG_KEYWORDS_FILE.exists() or DG_KEYWORDS_FILE.is_dir():
        return None
    try:
        stat = DG_KEYWORDS_FILE.stat()
        if _KEYWORD_CACHE is not None and _KEYWORD_MTIME == stat.st_mtime:
            return _KEYWORD_CACHE
        with DG_KEYWORDS_FILE.open(encoding="utf-8") as f:
            entries = [line.strip() for line in f if line.strip() and not line.strip().startswith("#")]
        _KEYWORD_CACHE = entries
        _KEYWORD_MTIME = stat.st_mtime
        if DG_DEBUG:
            print(f"[DG] loaded {len(entries)} keywords from {DG_KEYWORDS_FILE}")
        return entries
    except Exception as exc:
        print(f"[DG] keyword file read failed: {exc}")
        _KEYWORD_CACHE = None
        _KEYWORD_MTIME = None
        return None


def _current_keywords() -> List[str]:
    file_keywords = _load_keywords_from_file()
    if file_keywords is not None:
        items = file_keywords
    else:
        items = _inline_keywords()

    if DG_KEYWORDS_LIMIT and len(items) > DG_KEYWORDS_LIMIT:
        if DG_DEBUG:
            print(f"[DG] trimming keywords {len(items)} → {DG_KEYWORDS_LIMIT}")
        return items[:DG_KEYWORDS_LIMIT]
    return items


def _build_keyterm_list(
    org_custom: Optional[List[str]] = None,
    sermon_vocab: Optional[List[str]] = None,
    limit: int = 100,
) -> List[str]:
    """
    Merge keyterm tiers (priority: org_custom > sermon_vocab > defaults).
    Returns a deduplicated list up to `limit` items.

    Tier 1 — org_custom:   per-church terms (pastor name, series title, etc.)
    Tier 2 — sermon_vocab: Korean tokens extracted from today's sermon script
    Tier 3 — defaults:     _current_keywords() (worship terms + all 66 Bible books)
    """
    seen: set = set()
    result: List[str] = []

    def _add(terms: List[str]) -> None:
        for t in terms:
            t = (t or "").strip()
            if not t:
                continue
            # Dedup on the bare term only (strip any :boost suffix)
            bare = t.partition(":")[0].strip().lower()
            if not bare:
                continue
            if bare not in seen and len(result) < limit:
                seen.add(bare)
                result.append(t)

    _add(org_custom or [])
    _add(sermon_vocab or [])
    _add(_current_keywords())

    if DG_DEBUG:
        print(f"[DG] _build_keyterm_list: {len(result)} total "
              f"(org={len(org_custom or [])}, sermon={len(sermon_vocab or [])}, "
              f"defaults up to {limit - len(org_custom or []) - len(sermon_vocab or [])})")
    return result


def _build_replace_list(
    org_custom: Optional[List[Tuple[str, str]]] = None,
    limit: int = 200,
) -> List[Tuple[str, str]]:
    """
    Merge replace tiers (priority: org_custom > defaults).
    Dedup on the 'find' key (lowercased). Returns list of (find, replacement) tuples.

    Tier 1 — org_custom:  per-church corrections (admin-defined)
    Tier 2 — defaults:    DEFAULT_DEEPGRAM_REPLACEMENTS (known nova-3 patterns)
    """
    seen: set = set()
    result: List[Tuple[str, str]] = []

    def _add(pairs: List[Tuple[str, str]]) -> None:
        for find, replacement in pairs:
            find = _clean_deepgram_text(find, allow_colon=False)
            replacement = _clean_deepgram_text(replacement)
            if not find:
                continue
            key = find.lower()
            if key not in seen and len(result) < limit:
                seen.add(key)
                result.append((find, replacement))

    _add(org_custom or [])
    _add(DEFAULT_DEEPGRAM_REPLACEMENTS)

    if DG_DEBUG:
        print(f"[DG] _build_replace_list: {len(result)} pairs "
              f"(org={len(org_custom or [])}, defaults={len(DEFAULT_DEEPGRAM_REPLACEMENTS)})")
    return result


def _append_with_url_budget(
    params: List[Tuple[str, str]],
    name: str,
    values: List[str],
    *,
    endpoint: str = DG_ENDPOINT,
) -> int:
    added = 0
    for value in values:
        candidate = params + [(name, value)]
        candidate_url = f"{endpoint}?{urlencode(candidate, doseq=True)}"
        if len(candidate_url) > DG_MAX_URL_CHARS:
            break
        params.append((name, value))
        added += 1
    return added


def _qs(
    model: str,
    language: str,
    sample_rate: int,
    keywords: Optional[List[str]],
    endpointing_ms: Optional[int],
    utter_end_ms: Optional[int],
    replacements: Optional[List[Tuple[str, str]]] = None,
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

    # Repeated 'keywords' with a boost works on nova-2/enhanced/base
    if normalized_keywords and model in ("nova-2", "enhanced", "base"):
        values = [f"{term}:{boost or '3'}" for term, boost in normalized_keywords]
        added = _append_with_url_budget(params, "keywords", values)
        if DG_DEBUG and added < len(values):
            print(f"[DG] trimmed keywords by URL budget: {len(values)} -> {added}")

    # If someone flips to nova-3 later, map keywords -> keyterm (nova-3 style)
    if model.startswith("nova-3") and normalized_keywords:
        values = [_clean_deepgram_text(term, allow_colon=False) for term, _ in normalized_keywords]
        values = [term for term in values if term]
        added = _append_with_url_budget(params, "keyterm", values)
        if DG_DEBUG and added < len(values):
            print(f"[DG] trimmed keyterms by URL budget: {len(values)} -> {added}")

    # Find-and-replace: post-processing corrections (nova-3, not Flux)
    if replacements:
        values = [
            f"{find}:{replacement}"
            for find, replacement in _build_replace_list(replacements, limit=200)
        ]
        added = _append_with_url_budget(params, "replace", values)
        if DG_DEBUG and added < len(values):
            print(f"[DG] trimmed replacements by URL budget: {len(values)} -> {added}")

    return urlencode(params, doseq=True)


def _deepgram_connect_error(exc: Exception) -> str:
    if isinstance(exc, InvalidStatus):
        response = exc.response
        parts = [f"server rejected WebSocket connection: HTTP {response.status_code}"]
        request_id = response.headers.get("dg-request-id") or response.headers.get("request-id")
        if request_id:
            parts.append(f"request_id={request_id}")
        body = getattr(response, "body", b"") or b""
        if body:
            detail = body.decode("utf-8", errors="replace").strip()
            if detail:
                parts.append(detail[:500])
        return " | ".join(parts)

    if isinstance(exc, InvalidStatusCode):
        parts = [f"server rejected WebSocket connection: HTTP {exc.status_code}"]
        request_id = exc.headers.get("dg-request-id") or exc.headers.get("request-id")
        if request_id:
            parts.append(f"request_id={request_id}")
        return " | ".join(parts)

    return str(exc)


def deepgram_model_for_language(language: str) -> str:
    """Return the appropriate Deepgram model for the given language code.

    nova-3 does not support Chinese; fall back to nova-2 for those codes.
    Callers can override both models via DEEPGRAM_MODEL / DEEPGRAM_MODEL_FALLBACK env vars.
    """
    lang_key = (language or "").strip().lower()
    if DG_MODEL.startswith("nova-3") and lang_key in _NOVA3_UNSUPPORTED:
        return DG_MODEL_FALLBACK
    return DG_MODEL


async def connect_to_deepgram(
    model: Optional[str] = None,
    language: Optional[str] = None,
    keywords: Optional[List[str]] = None,
    sample_rate: int = 48000,
    replacements: Optional[List[Tuple[str, str]]] = None,
):
    if not DG_KEY:
        raise RuntimeError("DEEPGRAM_API_KEY not set")

    m  = model or deepgram_model_for_language(language or DG_LANGUAGE)
    lg = language or DG_LANGUAGE

    # Use keyword biasing only for Korean; avoid skewing other languages.
    kw = keywords if keywords is not None else _current_keywords()
    if lg and not str(lg).lower().startswith("ko"):
        kw = []

    url = f"{DG_ENDPOINT}?{_qs(m, lg, sample_rate, kw, DG_ENDPOINTING_MS, DG_UTTER_END_MS, replacements)}"

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
        try:
            return await ws_legacy.client.connect(
                url,
                extra_headers=headers,
                ping_interval=20,
                open_timeout=20,
                max_size=None,
            )
        except Exception as exc:
            raise RuntimeError(_deepgram_connect_error(exc)) from exc
    except Exception as exc:
        raise RuntimeError(_deepgram_connect_error(exc)) from exc

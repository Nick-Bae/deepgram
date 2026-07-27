import re


SERMON_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?。？！…])\s+|\n{2,}")
_TRAILING_PUNCTUATION_RE = re.compile(r"[\s,，:;·.!?。？！…]+$")
_QUOTE_THEN_EXPLANATION_RE = re.compile(
    r"^(?P<quote>[“\"'‘「『][^”\"'’」』]+[.!?。？！…][”\"'’」』])\s+(?P<rest>\S.+)$"
)
_STAGE_DIRECTION_RE = re.compile(
    r"(\((?=[^)]*(?:잠시|멈춤|침묵|pause|Pause|PAUSE|강단|지시))[^)]{1,80}\)"
    r"|\[(?=[^\]]*(?:잠시|멈춤|침묵|pause|Pause|PAUSE|강단|지시))[^\]]{1,80}\])"
)
_PARALLEL_CONNECTIVE_RE = re.compile(
    r"(?:고|시고|으며|시며|며|면서|아서|어서|서|지만|는데)$"
)
_INCOMPLETE_AUXILIARY_RE = re.compile(
    r"(?:"
    r"\S+\s+수(?:\s+(?:있|없)(?:기)?)?|"
    r"(?:지|지는|지도)\s+(?:않|못)(?:고|아|아서|으면|기)?"
    r")$"
)
_STRONGLY_INCOMPLETE_ENDING_RE = re.compile(
    r"(?:"
    r"은|는|이|가|을|를|의|에|에서|에게|한테|께|로|으로|와|과|도|만|까지|부터|"
    r"처럼|보다|마다|조차|마저|밖에|"
    r"고|며|면서|면|으면|지만|는데|은데|다가|도록|려고|으려고|거나|든지|"
    r"지|"
    r"때문에|위해|통해|대해|대한|따라|없이|"
    r"있는|없는|하는|되는|된|할|한|될|일"
    r")$"
)
_STANDALONE_DA_ENDING_RE = re.compile(r"(?:^|\s)다$")
_LONG_PARALLEL_SPLIT_MIN_CHARS = 55
_PARALLEL_CLAUSE_MIN_CHARS = 10


def ends_with_standalone_da(text: str) -> bool:
    """Return True when STT ended with a detached Korean '다' token."""
    clean = _TRAILING_PUNCTUATION_RE.sub("", (text or "").strip())
    if not clean:
        return False
    return bool(_STANDALONE_DA_ENDING_RE.search(clean))


def is_strongly_incomplete_korean_segment(text: str) -> bool:
    """Return True when a Korean STT segment clearly needs a continuation."""
    clean = _TRAILING_PUNCTUATION_RE.sub("", (text or "").strip())
    if not clean:
        return False
    return bool(
        _INCOMPLETE_AUXILIARY_RE.search(clean)
        or _STRONGLY_INCOMPLETE_ENDING_RE.search(clean)
    )


def join_korean_stt_segments(previous: str, current: str) -> str:
    """Join consecutive Deepgram segments without duplicating cumulative text."""
    prev = " ".join((previous or "").split())
    cur = " ".join((current or "").split())
    if not prev:
        return cur
    if not cur:
        return prev
    if cur.startswith(prev):
        return cur
    if prev.endswith(cur):
        return prev
    return f"{prev} {cur}"


def normalize_sermon_text_for_segmentation(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # Treat single newlines as soft wraps from pasted documents.
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n[ \t]+", "\n", text)
    return text


def split_sermon_korean_text(raw: str, *, auto_split: bool = True) -> list[str]:
    text = (raw or "").strip()
    if not text:
        return []
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not auto_split:
        return [line.strip() for line in text.splitlines() if line.strip()]

    normalized = normalize_sermon_text_for_segmentation(text)
    primary = [
        part.strip()
        for part in SERMON_SENTENCE_SPLIT_RE.split(normalized)
        if part.strip()
    ]

    segments: list[str] = []
    for part in primary:
        segments.extend(_split_sermon_segment_by_role_and_breath(part))
    return segments


def _split_sermon_segment_by_role_and_breath(segment: str) -> list[str]:
    pieces: list[str] = [segment]
    for splitter in (
        _split_stage_directions,
        _split_scripture_quote_from_explanation,
        _split_long_parallel_clause,
    ):
        next_pieces: list[str] = []
        for piece in pieces:
            next_pieces.extend(splitter(piece))
        pieces = next_pieces
    return [piece.strip() for piece in pieces if piece.strip()]


def _split_stage_directions(segment: str) -> list[str]:
    if not _STAGE_DIRECTION_RE.search(segment):
        return [segment]
    parts = [
        part.strip()
        for part in _STAGE_DIRECTION_RE.split(segment)
        if part and part.strip()
    ]
    return parts or [segment]


def _split_scripture_quote_from_explanation(segment: str) -> list[str]:
    match = _QUOTE_THEN_EXPLANATION_RE.match(segment.strip())
    if not match:
        return [segment]
    quote = match.group("quote").strip()
    rest = match.group("rest").strip()
    return [quote, rest] if quote and rest else [segment]


def _split_long_parallel_clause(segment: str) -> list[str]:
    compact_len = len(re.sub(r"\s+", "", segment))
    if compact_len < _LONG_PARALLEL_SPLIT_MIN_CHARS:
        return [segment]

    pieces = re.findall(r"[^,，]+[,，]?", segment)
    if len(pieces) < 2:
        return [segment]

    result: list[str] = []
    current = ""
    did_split = False
    for index, raw_piece in enumerate(pieces):
        piece = raw_piece.strip()
        if not piece:
            continue
        current = f"{current} {piece}".strip() if current else piece
        is_last = index == len(pieces) - 1
        if is_last:
            continue

        clean = _TRAILING_PUNCTUATION_RE.sub("", current)
        current_len = len(re.sub(r"\s+", "", current))
        if (
            current_len >= _PARALLEL_CLAUSE_MIN_CHARS
            and _PARALLEL_CONNECTIVE_RE.search(clean)
        ):
            result.append(current)
            current = ""
            did_split = True

    if current:
        result.append(current)
    return result if did_split and len(result) > 1 else [segment]

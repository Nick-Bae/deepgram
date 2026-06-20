import re


_TRAILING_PUNCTUATION_RE = re.compile(r"[\s,，:;·…]+$")
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

import re

_PARTICLE = re.compile(r"([은는이가을를도만에에서으로|로|에게|께|께서])$")

# Trailing Korean postpositional particles that Deepgram STT frequently drops
# or swaps under fast/noisy speech (은↔을, 의 dropped, 를/로 dropped). Longer
# multi-char particles listed first so the regex doesn't leave stragglers.
# Consonant-final endings like 다/까/네/데 are NOT particles and stay.
_TAIL_PARTICLE = re.compile(
    r"(으로|에서|에게|한테|까지|부터|만큼|처럼|보다|이나|같이|께서|"
    r"은|는|이|가|을|를|의|로|에|와|과|도|만|나|께)$"
)


def strip_ko_particles(text: str) -> str:
    """Strip one trailing Korean postpositional particle from each word.

    Deepgram STT hears '예수님을' and '예수님은' (particle 을↔은 swap)
    interchangeably when the pastor speaks fast. It also drops single-char
    particles like 의 and 로 entirely. Comparing sentences after stripping
    trailing particles gives a matcher that survives these STT variations
    without touching content-bearing morphemes.

    Whitespace is collapsed — the returned string has no spaces so callers
    can use it directly for substring comparison.

    Examples:
        예수님을 이야기의 한가운데로 모셔옵니다
          → 예수님이야기한가운데모셔옵니다
        예수님은 이야기 한가운데 모셔옵니다  (Deepgram-misheard version)
          → 예수님이야기한가운데모셔옵니다  (same — matches)
    """
    if not text:
        return ""
    return "".join(_TAIL_PARTICLE.sub("", w) for w in text.split())
_CONNECTIVE = re.compile(r"(면$|는데$|지만$|려고$|거나$|니까$|면서$|고$)")
_REL_PRENOM = re.compile(r"(ㄴ$|은$|는$|던$|을$|ㄹ$)")
_PUNCT = re.compile(r"[.,!?…‥·、，]")
# backend/app/utils/hangul.py
_SAFE = re.compile(
    r"^(오늘|지금|잠시후|곧|여기서|이곳에서|예배(에|에서)?|기도(에|에서)?|설교(후|전)?|말씀(후|전)?|"
    r"환영합니다?|안내(를|에)?|광고(를|에)?|헌금|축도(후|전)?|찬양(후|전)?|다음은|"
    r"아침|오전|오후|저녁|밤|주일|이번주|다음주|금요일|토요일|주말)$"
)



def tokenize_ko(s: str) -> list[str]:
    s = re.sub(r"[\t\n]+", " ", s)
    s = re.sub(r"\s{2,}", " ", s)
    s = s.strip()
    return [t for t in s.split(" ") if t]


def is_safe_adverbial(tok: str) -> bool: return bool(_SAFE.match(tok))

def has_particle(tok: str) -> bool: return bool(_PARTICLE.search(tok))

def looks_connective(tok: str) -> bool: return bool(_CONNECTIVE.search(tok))

def looks_rel_prenom(tok: str) -> bool: return bool(_REL_PRENOM.search(tok))

def has_punct(s: str) -> bool: return bool(_PUNCT.search(s))
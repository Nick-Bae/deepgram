"""Lightweight Korean spacing helper (no external deps).

Approach:
- Greedy longest-match segmentation using a small worship-focused word list.
- Falls back gracefully if nothing matches (returns original text).
- Only runs when the input has no spaces and is mostly Hangul to avoid harming
  already spaced text or mixed-language snippets.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

_HANGUL_RE = re.compile(r"[\uac00-\ud7a3]")
_HANGUL_RUN_RE = re.compile(r"[\uac00-\ud7a3]+")
_TOKEN_SPLIT_RE = re.compile(r"([,.;!?·…]|\s+)")

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"
_WORDLIST_PATH = _DATA_DIR / "ko_spacing_words.txt"


@lru_cache(maxsize=1)
def _word_set() -> set[str]:
    fallback = {
        # Core liturgy & worship terms
        "하나님", "예수님", "성령님", "주님", "말씀", "기도", "찬양", "예배", "봉헌",
        "사도신경", "축도", "찬송", "찬송가", "성경", "복음", "교회", "성도", "형제",
        "자매", "성가대", "헌금", "말씀을", "기도를", "찬양을", "예배를",
        "예배하며", "예배합시다", "예배드리며", "예배드립시다",
        "함께", "다 같이", "같이", "모두", "우리", "우리가", "우리의", "여러분",
        "형제자매", "가족", "주께", "주께서", "주님께", "주님께서",
        "일어나", "일어나서", "일어나셔서", "자리에서", "앉아", "앉아서",
        "축복", "축복합니다", "선포", "선포합니다", "기도합시다", "기도하겠습니다",
        "이 시간", "지금", "오늘", "오늘도", "다시", "다 함께", "주 앞에", "주님 앞에",
        "임재", "은혜", "사랑", "영광", "감사", "회개", "구원",

        # Function words / particles / auxiliaries
        "그리고", "그러나", "또", "또한", "그래서", "그러므로", "왜냐하면", "하지만",
        "때", "때에", "때마다", "것", "것을", "것을", "것도", "것은", "것이",
        "모든", "모두", "다", "다시", "다음에", "함께", "같이", "서로",
        "에게", "에게서", "께", "께서", "에서", "으로", "로", "까지", "부터",
        "보다", "보다도", "조차", "마저", "도", "만", "만큼", "이나", "나",
        "과", "와", "및", "의", "에", "에서", "에게", "께", "께서", "으로",
        "로", "처럼", "같이", "동안", "위해", "위하여", "위해서", "안에서", "밖에서",

        # Common verbs/adjectives in worship context
        "받으시게", "받으시는", "받으시는하나님", "들으시고", "들으시는", "나갈", "나갈 때",
        "예배하며", "예배하며 나갈", "예배하며 나갈 때", "고백하며", "선포하며",
        "찬양하며", "기억하며", "돌아보며", "감사하며",
    }

    wordset: set[str] = set()
    if _WORDLIST_PATH.exists():
        try:
            with _WORDLIST_PATH.open(encoding="utf-8") as fh:
                for line in fh:
                    token = line.strip()
                    if token and not token.startswith("#"):
                        wordset.add(token)
        except Exception:
            wordset = set()

    if not wordset:
        wordset = fallback

    # Add single-character particles to keep them separated
    wordset.update({"을", "를", "이", "가", "은", "는", "에", "도", "와", "과"})
    # Include spaced variants as single tokens for matching after normalization
    spaced = {w for w in list(wordset) if " " in w}
    wordset.update({w.replace(" ", "") for w in spaced})
    return wordset


def _is_mostly_hangul(text: str, threshold: float = 0.6) -> bool:
    if not text:
        return False
    total = len(text)
    hangul = len(_HANGUL_RE.findall(text))
    return total > 0 and (hangul / total) >= threshold


def _segment_run(run: str, max_len: int = 8) -> str:
    words = _word_set()
    out: list[str] = []
    i = 0
    n = len(run)
    while i < n:
        match = None
        upper = min(max_len, n - i)
        for length in range(upper, 0, -1):
            chunk = run[i : i + length]
            if chunk in words:
                match = chunk
                break
        if match:
            out.append(match)
            i += len(match)
        else:
            out.append(run[i])
            i += 1
    return " ".join(out)


def apply_ko_spacing(text: str) -> str:
    """Return text with best-effort Korean spacing.

    Only applies when:
    - text is mostly Hangul, and
    - text has no existing spaces.
    Otherwise returns the original text unchanged.
    """

    if not text:
        return text
    if " " in text:
        return text
    if not _is_mostly_hangul(text):
        return text

    parts = _TOKEN_SPLIT_RE.split(text)
    spaced_parts: list[str] = []
    for part in parts:
        if not part or part.isspace():
            continue
        if _HANGUL_RUN_RE.fullmatch(part):
            spaced_parts.append(_segment_run(part))
        else:
            spaced_parts.append(part)
    return " ".join(spaced_parts)


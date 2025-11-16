from __future__ import annotations

import json
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Optional

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
KO_REV_PATH = DATA_DIR / "ko_rev.json"
ESV_PATH = DATA_DIR / "esv.json"
TARGET_VERSION_LABEL = "English Standard Version (ESV)"
SOURCE_VERSION_LABEL = "Korean Revised Version (ko_rev)"

# Simple digit→Korean replacements so we can build alias variants like 요한일서.
_DIGIT_TO_KO = {
    "0": "영",
    "1": "일",
    "2": "이",
    "3": "삼",
    "4": "사",
    "5": "오",
    "6": "육",
    "7": "칠",
    "8": "팔",
    "9": "구",
}

_HANGUL_DIGIT_MAP = {
    "영": 0,
    "공": 0,
    "일": 1,
    "한": 1,
    "이": 2,
    "두": 2,
    "둘": 2,
    "삼": 3,
    "세": 3,
    "셋": 3,
    "사": 4,
    "네": 4,
    "넷": 4,
    "오": 5,
    "육": 6,
    "륙": 6,
    "칠": 7,
    "팔": 8,
    "구": 9,
}

_HANGUL_UNIT_MAP = {
    "십": 10,
    "열": 10,
    "백": 100,
    "천": 1000,
}

# Hand-tuned aliases for the few books whose spoken names rarely match the JSON key exactly.
_MANUAL_ALIAS_OVERRIDES = {
    "요한일서": "요한1서",
    "요한이서": "요한2서",
    "요한삼서": "요한3서",
    "사무엘상": "사무엘상",
    "사무엘하": "사무엘하",
    "삼상": "사무엘상",
    "삼하": "사무엘하",
    "왕상": "열왕기상",
    "왕하": "열왕기하",
    "대상": "역대상",
    "대하": "역대하",
    "고전": "고린도전서",
    "고후": "고린도후서",
    "살전": "데살로니가전서",
    "살후": "데살로니가후서",
    "딤전": "디모데전서",
    "딤후": "디모데후서",
    "벧전": "베드로전서",
    "벧후": "베드로후서",
    "벧전서": "베드로전서",
    "벧후서": "베드로후서",
    "빌렘몬서": "빌레몬서",
    "요한계시록": "요한계시록",
    "계시록": "요한계시록",
    "요한복음서": "요한복음",
    "마태복음서": "마태복음",
    "마가복음서": "마가복음",
    "누가복음서": "누가복음",
}

KOR_TO_ENG_BOOK = {
    "창세기": "Genesis",
    "출애굽기": "Exodus",
    "레위기": "Leviticus",
    "민수기": "Numbers",
    "신명기": "Deuteronomy",
    "여호수아": "Joshua",
    "사사기": "Judges",
    "룻기": "Ruth",
    "사무엘상": "1 Samuel",
    "사무엘하": "2 Samuel",
    "열왕기상": "1 Kings",
    "열왕기하": "2 Kings",
    "역대상": "1 Chronicles",
    "역대하": "2 Chronicles",
    "에스라": "Ezra",
    "느헤미야": "Nehemiah",
    "에스더": "Esther",
    "욥기": "Job",
    "시편": "Psalms",
    "잠언": "Proverbs",
    "전도서": "Ecclesiastes",
    "아가": "Song of Solomon",
    "이사야": "Isaiah",
    "예레미야": "Jeremiah",
    "예레미야애가": "Lamentations",
    "에스겔": "Ezekiel",
    "다니엘": "Daniel",
    "호세아": "Hosea",
    "요엘": "Joel",
    "아모스": "Amos",
    "오바댜": "Obadiah",
    "요나": "Jonah",
    "미가": "Micah",
    "나훔": "Nahum",
    "하박국": "Habakkuk",
    "스바냐": "Zephaniah",
    "학개": "Haggai",
    "스가랴": "Zechariah",
    "말라기": "Malachi",
    "마태복음": "Matthew",
    "마가복음": "Mark",
    "누가복음": "Luke",
    "요한복음": "John",
    "사도행전": "Acts",
    "로마서": "Romans",
    "고린도전서": "1 Corinthians",
    "고린도후서": "2 Corinthians",
    "갈라디아서": "Galatians",
    "에베소서": "Ephesians",
    "빌립보서": "Philippians",
    "골로새서": "Colossians",
    "데살로니가전서": "1 Thessalonians",
    "데살로니가후서": "2 Thessalonians",
    "디모데전서": "1 Timothy",
    "디모데후서": "2 Timothy",
    "디도서": "Titus",
    "빌레몬서": "Philemon",
    "히브리서": "Hebrews",
    "야고보서": "James",
    "베드로전서": "1 Peter",
    "베드로후서": "2 Peter",
    "요한1서": "1 John",
    "요한2서": "2 John",
    "요한3서": "3 John",
    "유다서": "Jude",
    "요한계시록": "Revelation",
}


@dataclass(frozen=True)
class ScriptureResult:
    book: str
    chapter: int
    verse: int
    end_verse: int
    text: str
    reference: str
    version: str
    book_en: Optional[str] = None
    reference_en: Optional[str] = None
    source_text: Optional[str] = None
    source_reference: Optional[str] = None
    source_version: str = SOURCE_VERSION_LABEL


def detect_scripture_verse(text: str) -> Optional[ScriptureResult]:
    """Return a ScriptureResult if the text contains a recognizable reference."""
    if not text:
        return None

    index = _get_index()
    normalized_text = _normalize_hangul_reference_text(text)
    match = index.pattern.search(normalized_text)
    if not match:
        return None

    alias = match.group("book")
    canonical = index.alias_to_book.get(alias)
    if not canonical:
        return None

    try:
        chapter = int(match.group("chapter"))
        verse = int(match.group("verse"))
    except (TypeError, ValueError):
        return None

    end_raw = match.group("endverse")
    end_verse = int(end_raw) if end_raw else verse
    if end_verse < verse:
        end_verse = verse

    book_entry = index.books.get(canonical)
    if not book_entry:
        return None

    verses_ko = _slice_verses(book_entry.get("chapters"), chapter, verse, end_verse)
    if not verses_ko:
        return None

    ko_text = " ".join(verses_ko).strip()
    ref_ko = _format_reference(book_entry["name"], chapter, verse, end_verse)

    english_name = index.book_to_english.get(canonical)
    en_text = None
    ref_en = None
    if english_name:
        verses_en = _slice_esv(index.english_books, english_name, chapter, verse, end_verse)
        if verses_en:
            en_text = " ".join(verses_en).strip()
            ref_en = _format_reference(english_name, chapter, verse, end_verse)

    primary_text = en_text or ko_text
    if not primary_text:
        return None
    primary_ref = ref_en or ref_ko
    version_label = TARGET_VERSION_LABEL if en_text else SOURCE_VERSION_LABEL

    return ScriptureResult(
        book=book_entry["name"],
        chapter=chapter,
        verse=verse,
        end_verse=end_verse,
        text=primary_text,
        reference=primary_ref,
        version=version_label,
        book_en=english_name,
        reference_en=ref_en,
        source_text=ko_text or None,
        source_reference=ref_ko,
    )


def _slice_verses(
    chapters: Optional[Iterable[Iterable[str]]],
    chapter: int,
    verse: int,
    end_verse: int,
) -> Optional[list[str]]:
    if not chapters:
        return None
    if chapter < 1 or chapter > len(chapters):
        return None
    chapter_data = chapters[chapter - 1]
    if not chapter_data:
        return None
    if verse < 1 or verse > len(chapter_data):
        return None
    end = min(len(chapter_data), max(verse, end_verse))
    selection = [chapter_data[i].strip() for i in range(verse - 1, end)]
    return selection if selection else None


def _format_reference(book: str, chapter: int, verse: int, end_verse: int) -> str:
    base = f"{book} {chapter}:{verse}"
    if end_verse > verse:
        base = f"{base}-{end_verse}"
    return base


class _ScriptureIndex:
    __slots__ = ("books", "alias_to_book", "pattern", "english_books", "book_to_english")

    def __init__(self, books, alias_to_book, pattern: re.Pattern[str], english_books, book_to_english):
        self.books = books
        self.alias_to_book = alias_to_book
        self.pattern = pattern
        self.english_books = english_books
        self.book_to_english = book_to_english


@lru_cache(maxsize=1)
def _get_index() -> _ScriptureIndex:
    try:
        raw = json.loads(KO_REV_PATH.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise RuntimeError(f"ko_rev.json not found at {KO_REV_PATH}") from exc

    books = {entry["name"].strip(): entry for entry in raw}
    alias_to_book: dict[str, str] = {}
    for book_name in books:
        for alias in _aliases_for(book_name):
            alias_to_book.setdefault(alias, book_name)

    for alias, canonical in _MANUAL_ALIAS_OVERRIDES.items():
        if canonical in books:
            alias_to_book.setdefault(alias, canonical)

    if not alias_to_book:
        raise RuntimeError("No aliases available for scripture detection")

    try:
        english_books = _load_esv_books()
    except FileNotFoundError as exc:
        raise RuntimeError(f"esv.json not found at {ESV_PATH}") from exc

    book_to_english = {
        book: KOR_TO_ENG_BOOK.get(book)
        for book in books
    }

    pattern = _build_regex(alias_to_book.keys())
    return _ScriptureIndex(
        books=books,
        alias_to_book=alias_to_book,
        pattern=pattern,
        english_books=english_books,
        book_to_english=book_to_english,
    )


def _aliases_for(book_name: str) -> set[str]:
    aliases = {book_name}
    trimmed = book_name.replace(" ", "")
    aliases.add(trimmed)

    digit_variant = _replace_digits_with_korean(book_name)
    aliases.add(digit_variant)
    if book_name.endswith("복음"):
        aliases.add(f"{book_name}서")
    if not book_name.endswith("서") and len(book_name) <= 4:
        aliases.add(f"{book_name}서")

    # Remove accidental empty strings
    return {alias for alias in aliases if alias}


def _replace_digits_with_korean(value: str) -> str:
    out = []
    for ch in value:
        out.append(_DIGIT_TO_KO.get(ch, ch))
    return "".join(out)


def _load_esv_books() -> dict[str, dict[int, dict[int, str]]]:
    raw = json.loads(ESV_PATH.read_text(encoding="utf-8"))
    normalized: dict[str, dict[int, dict[int, str]]] = {}
    for book, chapters in raw.items():
        book_entry: dict[int, dict[int, str]] = {}
        for chapter_key, verses in chapters.items():
            chapter_index = int(chapter_key)
            verse_map: dict[int, str] = {}
            for verse_key, verse_text in verses.items():
                verse_map[int(verse_key)] = verse_text.strip()
            book_entry[chapter_index] = verse_map
        normalized[book] = book_entry
    return normalized


def _slice_esv(
    books: dict[str, dict[int, dict[int, str]]],
    book_name: str,
    chapter: int,
    verse: int,
    end_verse: int,
) -> Optional[list[str]]:
    book = books.get(book_name)
    if not book:
        return None
    chapter_map = book.get(chapter)
    if not chapter_map:
        return None
    result: list[str] = []
    for idx in range(verse, end_verse + 1):
        text = chapter_map.get(idx)
        if text:
            result.append(text.strip())
    return result or None


_HANGUL_NUM_CHARS = "".join(sorted({*"".join(_HANGUL_DIGIT_MAP.keys()), *"".join(_HANGUL_UNIT_MAP.keys())}))
_HANGUL_NUM_PATTERN = re.compile(rf"(?P<num>[{_HANGUL_NUM_CHARS}]+)\s*(?P<unit>장|절)")


def _normalize_hangul_reference_text(text: str) -> str:
    if not text:
        return text

    def repl(match: re.Match[str]) -> str:
        raw = match.group("num")
        unit = match.group("unit")
        value = _hangul_to_int(raw)
        if value is None:
            return match.group(0)
        return f"{value}{unit}"

    return _HANGUL_NUM_PATTERN.sub(repl, text)


def _hangul_to_int(token: str) -> Optional[int]:
    token = (token or "").strip()
    if not token:
        return None

    total = 0
    num = 0
    for ch in token:
        if ch in _HANGUL_UNIT_MAP:
            unit = _HANGUL_UNIT_MAP[ch]
            coeff = num or 1
            total += coeff * unit
            num = 0
        elif ch in _HANGUL_DIGIT_MAP:
            num = _HANGUL_DIGIT_MAP[ch]
        else:
            return None

    total += num
    if total <= 0:
        return None
    return total


def _build_regex(aliases: Iterable[str]) -> re.Pattern[str]:
    escaped = sorted({re.escape(alias) for alias in aliases}, key=len, reverse=True)
    pattern = "|".join(escaped)
    # Accept formats like "요한복음 3장 16절", "요한복음3:16", or "요한복음3장16-18절".
    regex = re.compile(
        rf"(?P<book>{pattern})\s*(?P<chapter>\d{{1,3}})\s*(?:장)?\s*(?:[:\.\s]?\s*)?"
        rf"(?P<verse>\d{{1,3}})\s*(?:절)?(?:\s*(?:-|~|부터|에서)\s*(?P<endverse>\d{{1,3}})\s*(?:절)?(?:\s*까지)?)?",
        re.UNICODE,
    )
    return regex

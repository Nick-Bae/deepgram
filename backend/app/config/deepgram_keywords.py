# backend/app/config/deepgram_keywords.py
from typing import List, Tuple
#
# Keyterms for Deepgram nova-3 STT.
# nova-3 uses ?keyterm=term (no boost) — boost values are NOT supported and were removed.
# nova-2/enhanced/base still receive these as ?keywords=term:boost (boost defaults to 3 in deepgram_session.py).
#
# Budget: nova-3 supports up to 100 keyterms total.
# This default list is 96 terms (30 worship + 66 Bible books), leaving 4 slots
# for per-org custom terms when combined with the 100-term cap.
# In practice, Tier 1 (org custom, up to 50) takes priority and fills first,
# so the default list fills only remaining slots.

_WORSHIP_TERMS = [
    "예수님", "하나님", "성령님", "할렐루야", "아멘",
    "주님", "예배", "찬양", "기도", "믿음",
    "말씀", "복음", "구원", "은혜", "십자가",
    "부활", "성경", "교회", "목사님", "사랑",
    "죄", "용서", "영광", "천국", "지옥",
    "성도", "예수", "그리스도", "주", "회개",
]  # 30 terms

_BIBLE_BOOKS = [
    "창세기", "출애굽기", "레위기", "민수기", "신명기",
    "여호수아", "사사기", "룻기", "사무엘상", "사무엘하",
    "열왕기상", "열왕기하", "역대상", "역대하", "에스라",
    "느헤미야", "에스더", "욥기", "시편", "잠언",
    "전도서", "아가", "이사야", "예레미야", "예레미야애가",
    "에스겔", "다니엘", "호세아", "요엘", "아모스",
    "오바댜", "요나", "미가", "나훔", "하박국",
    "스바냐", "학개", "스가랴", "말라기",
    "마태복음", "마가복음", "누가복음", "요한복음", "사도행전",
    "로마서", "고린도전서", "고린도후서", "갈라디아서", "에베소서",
    "빌립보서", "골로새서", "데살로니가전서", "데살로니가후서",
    "디모데전서", "디모데후서", "디도서", "빌레몬서", "히브리서",
    "야고보서", "베드로전서", "베드로후서", "요한일서", "요한이서",
    "요한삼서", "유다서", "요한계시록",
]  # 66 terms

DEFAULT_DEEPGRAM_KEYWORDS = _WORSHIP_TERMS + _BIBLE_BOOKS  # 96 total

# Known nova-3 Korean STT patterns that `replace` can fix post-recognition.
# Each tuple: (find, replacement). Deepgram matches 'find' case-insensitively.
DEFAULT_DEEPGRAM_REPLACEMENTS: List[Tuple[str, str]] = [
    ("할렐루아",  "할렐루야"),   # variant spelling (common nova-3 output)
    ("아-멘",     "아멘"),        # hyphenated pause artifact
    ("예수 님",   "예수님"),      # space inserted in honorific compound
    ("하나 님",   "하나님"),      # space inserted in honorific compound
    ("성령 님",   "성령님"),      # space inserted in honorific compound
    ("목사 님",   "목사님"),      # space inserted in honorific compound
    ("전도 사",   "전도사"),      # space in 전도사
    ("장로 님",   "장로님"),      # space in honorific
]

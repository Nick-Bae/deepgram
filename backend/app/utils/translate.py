# backend/app/utils/translate.py
import os
import json
import pathlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable, List, Optional
from collections import deque
from dotenv import load_dotenv

# OpenAI >= 1.0
from openai import AsyncOpenAI
from app.env import ENV
from app.utils.spacing import apply_ko_spacing

load_dotenv()

_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # fast & good; use gpt-4o if you want
_API_KEY = os.getenv("OPENAI_API_KEY")

_client: AsyncOpenAI | None = None
_CUSTOM_PROMPT_CACHE: dict[str, object] = {"mtime": None, "text": ""}
_SERVICE_PROMPT_CACHE: dict[str, object] = {"mtime": None, "text": ""}

@dataclass
class TranslationContext:
    subject: str = ENV.CONTEXT_SUBJECT
    pronoun: str = ENV.CONTEXT_PRONOUN
    narration_mode: str = ENV.CONTEXT_MODE
    last_english: Optional[str] = None

# Soft glossary for key theological terms (you can expand this)
THEOLOGICAL_TERMS: list[tuple[str, str]] = [
    ("여호와", "the LORD"),
    ("하나님 나라", "the kingdom of God"),
    ("언약", "covenant"),
    ("은혜", "grace"),
    ("의", "righteousness"),
    ("성령", "the Holy Spirit"),
    ("회개", "repentance"),
    ("구원", "salvation"),
    ("십자가", "the cross"),
    ("복음", "the gospel")
]

# Optional hard glossary (protected tokens). Defaults to the same set, but can be extended.
HARD_GLOSSARY_TERMS: list[tuple[str, str]] = THEOLOGICAL_TERMS.copy()

# Load Bible names map (Korean -> English) from JSON
_DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
_BIBLE_NAMES_PATH = _DATA_DIR / "bible_names.json"
_TRANSLATION_LOG_PATH = _DATA_DIR / "translation_examples.jsonl"
_CUSTOM_PROMPT_PATH = _DATA_DIR / "custom_prompt.txt"
_SERVICE_PROMPT_PATH = _DATA_DIR / "service_prompt.txt"

try:
    with open(_BIBLE_NAMES_PATH, encoding="utf-8") as f:
        BIBLE_NAMES: dict[str, str] = json.load(f)
    print(f"[TX] Loaded {len(BIBLE_NAMES)} Bible names from {__file__}")
except FileNotFoundError:
    print(f"[TX] bible_names.json not found at {_BIBLE_NAMES_PATH}; continuing without Bible name map")
    BIBLE_NAMES = {}

FIRST_PERSON_KO_MARKERS = [
    "나는", "난", "내가", "내게", "나를", "나도", "나만", "나와", "나에게", "나한테", "나의",
    "저는", "전", "제가", "제게", "저를", "저도", "저만", "저와", "저에게", "저한테", "저의",
    "우리", "우리가", "우리는", "우릴", "우리의", "우리도", "우리만", "우리와", "우리에게", "우리한테"
]

WE_KO_MARKERS = [
    "우리", "우리가", "우리는", "우릴", "우리의", "우리도", "우리만", "우리와", "우리에게", "우리한테",
]

HE_KO_MARKERS = [
    "그는", "그가", "그를", "그의", "그에게", "그한테",
    "그분은", "그분이", "그분을", "그분의",
]

SHE_KO_MARKERS = [
    "그녀는", "그녀가", "그녀를", "그녀의", "그녀에게", "그녀한테",
]

THEY_KO_MARKERS = [
    "그들은", "그들이", "그들을", "그들의", "그들에게", "그들한테",
]

IMPLICIT_FIRST_PERSON_KO_TERMS = [
    "아내", "남편", "부인", "배우자", "집사람", "마누라", "와이프",
]

IMPLICIT_FIRST_PERSON_BLOCKERS = [
    "그의", "그녀의", "그들의", "그분의", "누구의", "어떤", "한", "이런", "저런", "그", "이", "저",
    "남의", "타인의", "이웃의",
]

PRONOUN_FORMS = {
    "i": {
        "subject": "I",
        "object": "me",
        "possessive": "my",
        "reflexive": "myself",
        "be_present": "I am",
        "be_present_contracted": "I'm",
        "be_past": "I was",
        "have": "I have",
        "have_contracted": "I've",
        "will": "I will",
        "will_contracted": "I'll",
        "would": "I would",
        "would_contracted": "I'd",
        "can": "I can",
    },
    "he": {
        "subject": "He",
        "object": "him",
        "possessive": "his",
        "reflexive": "himself",
        "be_present": "He is",
        "be_present_contracted": "He's",
        "be_past": "He was",
        "have": "He has",
        "have_contracted": "He's",
        "will": "He will",
        "will_contracted": "He'll",
        "would": "He would",
        "would_contracted": "He'd",
        "can": "He can",
    },
    "she": {
        "subject": "She",
        "object": "her",
        "possessive": "her",
        "reflexive": "herself",
        "be_present": "She is",
        "be_present_contracted": "She's",
        "be_past": "She was",
        "have": "She has",
        "have_contracted": "She's",
        "will": "She will",
        "will_contracted": "She'll",
        "would": "She would",
        "would_contracted": "She'd",
        "can": "She can",
    },
    "they": {
        "subject": "They",
        "object": "them",
        "possessive": "their",
        "reflexive": "themselves",
        "be_present": "They are",
        "be_present_contracted": "They're",
        "be_past": "They were",
        "have": "They have",
        "have_contracted": "They've",
        "will": "They will",
        "will_contracted": "They'll",
        "would": "They would",
        "would_contracted": "They'd",
        "can": "They can",
    },
    "we": {
        "subject": "We",
        "object": "us",
        "possessive": "our",
        "reflexive": "ourselves",
        "be_present": "We are",
        "be_present_contracted": "We're",
        "be_past": "We were",
        "have": "We have",
        "have_contracted": "We've",
        "will": "We will",
        "will_contracted": "We'll",
        "would": "We would",
        "would_contracted": "We'd",
        "can": "We can",
    },
}


def _ensure_data_dir() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"[TX] failed to ensure data dir {_DATA_DIR}: {exc}")


def _get_custom_prompt() -> str:
    """Read admin-provided custom prompt text with mtime caching."""
    global _CUSTOM_PROMPT_CACHE
    try:
        stat = _CUSTOM_PROMPT_PATH.stat()
    except FileNotFoundError:
        _CUSTOM_PROMPT_CACHE = {"mtime": None, "text": ""}
        return ""
    mtime = stat.st_mtime
    if _CUSTOM_PROMPT_CACHE.get("mtime") == mtime:
        return str(_CUSTOM_PROMPT_CACHE.get("text", ""))

    try:
        text = _CUSTOM_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        text = ""
    _CUSTOM_PROMPT_CACHE = {"mtime": mtime, "text": text}
    return text


def _get_service_prompt() -> str:
    """Read per-service background prompt with mtime caching."""
    global _SERVICE_PROMPT_CACHE
    try:
        stat = _SERVICE_PROMPT_PATH.stat()
    except FileNotFoundError:
        _SERVICE_PROMPT_CACHE = {"mtime": None, "text": ""}
        return ""

    mtime = stat.st_mtime
    if _SERVICE_PROMPT_CACHE.get("mtime") == mtime:
        return str(_SERVICE_PROMPT_CACHE.get("text", ""))

    try:
        text = _SERVICE_PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        text = ""
    _SERVICE_PROMPT_CACHE = {"mtime": mtime, "text": text}
    return text


def _preprocess_source_text(text: str, source_lang: str) -> str:
    """Light, safe fixes for common Korean STT glitches that hurt translation.

    Keep this conservative: only normalize patterns we are confident about.
    """
    if not text or not source_lang.lower().startswith("ko"):
        return text

    cleaned = text
    replacements: list[tuple[str, str]] = [
        # Frequent STT slips during liturgy
        (r"사도신명", "사도신경"),
        (r"사도신병", "사도신경"),
        (r"이\\s*시간다", "이 시간 다"),
        (r"예배하며나갈때", "예배하며 나갈 때"),
        (r"함께예배하며나갈때", "함께 예배하며 나갈 때"),
        (r"하나님이계시기에", "하나님이 계시기에"),
    ]

    for pattern, repl in replacements:
        cleaned = re.sub(pattern, repl, cleaned)

    # Normalize missing space in "이시간" when it appears at the start
    cleaned = re.sub(r"^이시간", "이 시간", cleaned)
    return cleaned


def _log_translation_example(
    *,
    source_lang: str,
    target_lang: str,
    stt_text: str,
    auto_translation: str,
    final_translation: Optional[str] = None,
) -> None:
    """
    Append an example to translation_examples.jsonl so we can reuse it later.
    """
    if not stt_text or not auto_translation:
        return

    record = {
        "timestamp": datetime.utcnow().isoformat(timespec="milliseconds") + "Z",
        "source_lang": source_lang,
        "target_lang": target_lang,
        "stt_text": stt_text,
        "auto_translation": auto_translation,
        "final_translation": final_translation or auto_translation,
        "corrected": final_translation is not None,
    }

    try:
        _ensure_data_dir()
        with open(_TRANSLATION_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:
        print(f"[TX] Failed to log translation example: {exc}")


def log_corrected_translation(
    *,
    source_lang: str,
    target_lang: str,
    stt_text: str,
    auto_translation: str,
    final_translation: str,
) -> None:
    """
    Public helper for other modules to log manual corrections.
    """
    _log_translation_example(
        source_lang=source_lang,
        target_lang=target_lang,
        stt_text=stt_text,
        auto_translation=auto_translation,
        final_translation=final_translation,
    )


def _token_set(text: str) -> set[str]:
    # Light tokenization for overlap checks; ignores very short fragments.
    tokens = re.findall(r"[가-힣A-Za-z']+", text or "")
    return {t for t in tokens if len(t) >= 2}


def _load_fewshot_examples(
    source_lang: str,
    target_lang: str,
    *,
    max_examples: int = 3,
    current_source_text: Optional[str] = None,
) -> List[dict[str, str]]:
    """Return up to N on-topic, corrected examples.

    Filters to corrected rows only and requires minimal lexical overlap with the
    current source clause (if provided) to avoid off-topic bias.
    """
    if not _TRANSLATION_LOG_PATH.exists() or max_examples <= 0:
        return []

    corrected: deque[dict[str, str]] = deque(maxlen=max_examples)
    overlap_ref = _token_set(current_source_text) if current_source_text else None

    try:
        with open(_TRANSLATION_LOG_PATH, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue

                if record.get("source_lang") != source_lang or record.get("target_lang") != target_lang:
                    continue
                if not record.get("corrected"):
                    continue  # use only curated rows

                source_text = (record.get("stt_text") or "").strip()
                final_text = (record.get("final_translation") or record.get("auto_translation") or "").strip()
                if not source_text or not final_text:
                    continue

                if overlap_ref is not None:
                    if not _token_set(source_text) & overlap_ref:
                        continue  # skip off-topic examples

                corrected.append({"source": source_text, "target": final_text})
    except Exception as exc:
        print(f"[TX] Failed to read translation examples: {exc}")
        return []

    return list(corrected)[-max_examples:]


def _build_fewshot_block(source: str, target: str, *, current_source_text: Optional[str] = None) -> str:
    examples = _load_fewshot_examples(source, target, max_examples=3, current_source_text=current_source_text)
    if not examples:
        return ""

    source_name = _language_name(source)
    target_name = _language_name(target)
    formatted: list[str] = []
    for ex in examples:
        formatted.append(
            f"{source_name}: {ex['source']}\n"
            f"Preferred {target_name}: {ex['target']}"
        )

    return (
        "\nHere are recent on-topic corrections that show the desired style:\n\n"
        + "\n\n".join(formatted)
        + "\n"
    )


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not _API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set")
        _client = AsyncOpenAI(api_key=_API_KEY)
    return _client


def _language_name(code_or_name: str) -> str:
    """
    Map simple codes or names to a nice human-readable language label
    for the prompt. Falls back to the original value.
    """
    if not code_or_name:
        return "Unknown"

    key = code_or_name.strip().lower()

    mapping = {
        "ko": "Korean",
        "kr": "Korean",
        "한국어": "Korean",
        "korean": "Korean",

        "en": "English",
        "eng": "English",
        "영어": "English",
        "english": "English",

        "zh": "Chinese",
        "zh-cn": "Chinese (Simplified)",
        "zh-sg": "Chinese (Simplified)",
        "zh-tw": "Chinese (Traditional)",
        "zh-hk": "Chinese (Traditional)",
        "cn": "Chinese",
        "중국어": "Chinese",
        "chinese": "Chinese"
    }

    return mapping.get(key, code_or_name)


def _normalize_pronoun(ctx: Optional[TranslationContext]) -> Optional[str]:
    if not ctx:
        return None
    raw = (ctx.pronoun or ENV.CONTEXT_PRONOUN or "").strip().lower()
    if raw.startswith("i"):
        return "i"
    if raw.startswith("he"):
        return "he"
    if raw.startswith("she"):
        return "she"
    if raw.startswith("they"):
        return "they"
    if raw.startswith("we"):
        return "we"
    return None


def _contains_marker_list(text: str, markers: list[str]) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    for marker in markers:
        # Match only when the marker is not glued to adjacent Hangul to avoid false positives (e.g., '성전' contains '전').
        pattern = rf"(?<![가-힣]){re.escape(marker)}(?![가-힣])"
        if re.search(pattern, compact):
            return True
    return False


def _contains_first_person_markers(text: str) -> bool:
    return _contains_marker_list(text, FIRST_PERSON_KO_MARKERS)

def _contains_implicit_first_person_kinship(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return False
    if _detect_third_person_pronoun(compact):
        return False
    for term in IMPLICIT_FIRST_PERSON_KO_TERMS:
        idx = compact.find(term)
        if idx == -1:
            continue
        prefix = compact[max(0, idx - 6):idx]
        if any(prefix.endswith(block) for block in IMPLICIT_FIRST_PERSON_BLOCKERS):
            continue
        return True
    return False


def _contains_we_markers(text: str) -> bool:
    return _contains_marker_list(text, WE_KO_MARKERS)


def _clause_head(en: str) -> str:
    """
    Return the leading clause (up to the first sentence delimiter) so we can
    peek at the apparent subject without pulling in the whole paragraph.
    """
    clean = " ".join((en or "").split())
    if not clean:
        return ""
    head = re.split(r"[.!?;:\n]", clean, 1)[0]
    return head.strip()


def _infer_subject_from_english(
    en: str,
    default_subject: str,
    default_pronoun: str,
) -> tuple[str, str]:
    """
    Heuristic subject/pronoun detector from the previous English sentence.

    Goal: if the prior line already established a third-person subject
    (e.g., “those who…”, “they…”, “he…”) and the next Korean clause drops the
    subject, we want to keep using that subject instead of falling back to the
    default congregational “we”.
    """
    head = _clause_head(en)
    if not head:
        return default_subject, default_pronoun

    low = head.lower()
    if re.match(r"^(let's|let us)\b", low):
        return default_subject or ENV.CONTEXT_SUBJECT, "we"
    if re.match(r"^(i|my|me|myself)\b", low):
        return "I", "i"
    if re.match(r"^(we|our|us|ourselves)\b", low):
        return default_subject or ENV.CONTEXT_SUBJECT, "we"
    if re.match(r"^(they|those|these)\b", low):
        return head, "they"
    if re.match(r"^(he|his|him)\b", low):
        return head, "he"
    if re.match(r"^(she|her)\b", low):
        return head, "she"
    if re.match(r"^(jesus|christ|lord|god)\b", low):
        return head, "he"

    # Plural noun phrase (e.g., "the Levites", "the Pharisees") anywhere in the head
    plural_match = re.search(r"\b(the\s+[a-z][\w-]*s)\b", low)
    if plural_match:
        subj = head[plural_match.start():plural_match.end()]
        return subj.strip(), "they"

    if re.search(r"\blevites\b", low):
        return "the Levites", "they"

    # No strong signal: keep existing defaults.
    return default_subject, default_pronoun


def _format_replacement(match: re.Match, replacement: str) -> str:
    string = match.string
    idx = match.start()
    while idx > 0 and string[idx - 1].isspace():
        idx -= 1
    sentence_start = idx == 0 or string[idx - 1] in ".!?“”\"'‘’("
    if sentence_start:
        return replacement
    if replacement:
        return replacement[0].lower() + replacement[1:]
    return replacement

def _detect_third_person_pronoun(ko: str) -> str:
    compact = re.sub(r"\s+", "", ko)
    if any(marker in compact for marker in SHE_KO_MARKERS):
        return "she"
    if any(marker in compact for marker in THEY_KO_MARKERS):
        return "they"
    if any(marker in compact for marker in HE_KO_MARKERS):
        return "he"
    return ""


def _build_context_block(ctx: Optional[TranslationContext]) -> str:
    if not ctx:
        return ""
    subject = ctx.subject or ENV.CONTEXT_SUBJECT
    pronoun = ctx.pronoun or ENV.CONTEXT_PRONOUN
    narration = ctx.narration_mode or ENV.CONTEXT_MODE

    return (
        "\nSubject continuity:\n"
        f"- Main character: {subject} ({pronoun}). Keep dropped subjects anchored here.\n"
        f"- Narration mode: {narration}. Do not switch to first-person narration unless the Korean clause "
        "explicitly contains first-person markers (나/저/우리 variants) or implies speaker ownership via "
        "kinship terms (e.g., 아내/남편/부인/배우자).\n"
        "- If a kinship term (아내/남편/부인/배우자/집사람/와이프) appears without an explicit possessor "
        "(그의/그녀의/그들의), assume it refers to the speaker (e.g., \"my wife\").\n"
        "- When translating 스스로 or similar reflexives, use himself/herself/themselves matching the subject.\n"
    )

def _mask_hard_glossary(text: str, source_lang: str) -> tuple[str, dict[str, str]]:
    """
    Replace hard glossary Korean terms with stable tokens [[T1]], [[T2]]...
    Returns masked text and token->target map.
    Only applied for Korean source to avoid harming other languages.
    """
    if not text or not source_lang.lower().startswith("ko"):
        return text, {}

    masked = text
    mapping: dict[str, str] = {}
    for idx, (ko_term, en_term) in enumerate(HARD_GLOSSARY_TERMS, start=1):
        if not ko_term:
            continue
        token = f"[[T{idx}]]"
        if ko_term in masked:
            masked = masked.replace(ko_term, token)
            mapping[token] = en_term
    return masked, mapping

def _unmask_hard_glossary(text: str, mapping: dict[str, str]) -> str:
    out = text or ""
    for token, replacement in mapping.items():
        out = out.replace(token, replacement)
    return out


def _build_system_prompt(source: str, target: str, ctx: Optional[TranslationContext], *, current_source_text: Optional[str] = None) -> str:
    """
    Core system prompt: neutral/cautious worship captioning with domain aids.
    """
    source_name = _language_name(source)
    target_name = _language_name(target)

    # Theological glossary (only if from Korean)
    glossary_lines: list[str] = []
    if source_name == "Korean":
        for src, tgt in THEOLOGICAL_TERMS:
            glossary_lines.append(f'- Translate "{src}" as "{tgt}".')

    glossary_block = (
        "\nImportant terms (prefer these renderings):\n" + "\n".join(glossary_lines) + "\n"
    ) if glossary_lines else ""

    # Bible names list (Korean -> English)
    bible_name_lines: list[str] = []
    if source_name == "Korean" and BIBLE_NAMES:
        for ko_name, en_name in BIBLE_NAMES.items():
            bible_name_lines.append(f'- "{ko_name}" → "{en_name}"')

    bible_names_block = ""
    if bible_name_lines:
        bible_names_block = (
            "\nBiblical names/places (standard forms):\n" + "\n".join(bible_name_lines) + "\n"
        )

    fewshot_block = _build_fewshot_block(source, target, current_source_text=current_source_text)

    service_prompt = (_get_service_prompt() or "").strip()
    service_block = ("\nService background:\n" + service_prompt + "\n") if service_prompt else ""

    custom_prompt = (_get_custom_prompt() or "").strip()
    custom_block = ("\nGlobal guidance:\n" + custom_prompt + "\n") if custom_prompt else ""

    system = (
        "You are a professional translator for live church worship captions.\n"
        f"Translate {source_name} → {target_name} faithfully and naturally.\n"
        "\n"
        "Safety & neutrality:\n"
        "- Do NOT add meaning not present in the Korean; resolve ambiguity with the most neutral, church-appropriate reading.\n"
        "- Avoid slang, romantic/sexual nuance, or suggestive wording unless explicit in Korean.\n"
        "- Keep wording reverent and family-friendly for mixed ages.\n"
        "- Keep proper nouns; do not invent names or details.\n"
        "- If you see placeholder tokens like [[T1]], [[T2]], leave them unchanged; they will be mapped to glossary terms after translation.\n"
        "\n"
        "Style:\n"
        "- Clear, contemporary, pastoral tone.\n"
        "- Split overly long sentences for subtitle readability.\n"
        "- If input seems incomplete, translate only what is present; do not guess endings.\n"
        "\n"
        "STT robustness:\n"
        "- Input may come from speech recognition; if a word is clearly a mis-hear, quietly recover the intended Korean before translating.\n"
        "- Restore missing Korean spacing mentally.\n"
        "\n"
        "Congregation cues:\n"
        "- If the Korean says 일어나/일어나서/일어나셔서/자리에서 일어나 with no subject, interpret as an invitation to the congregation (e.g., \"let's stand\" / \"please stand\").\n"
        "\n"
        "Kinship cues:\n"
        "- If Korean mentions spouse/kinship terms (아내/남편/부인/배우자/집사람/와이프) without a possessor "
        "(그의/그녀의/그들의), treat them as the speaker's relation (\"my wife/husband\").\n"
        "\n"
        + bible_names_block
        + glossary_block
        + fewshot_block
        + service_block
        + custom_block
        + _build_context_block(ctx)
    )

    return system


def _enforce_subject_guardrails(en: str, source_text: str, ctx: Optional[TranslationContext]) -> str:
    implicit_first_person = _contains_implicit_first_person_kinship(source_text)
    if _contains_first_person_markers(source_text) and not implicit_first_person:
        return en

    pronoun_key = "i" if implicit_first_person else _normalize_pronoun(ctx)

    # If Korean explicitly uses third-person pronouns (그는/그녀는/그들은), honor that.
    explicit_pronoun = _detect_third_person_pronoun(source_text)
    if explicit_pronoun:
        pronoun_key = explicit_pronoun
        if ctx:
            ctx.pronoun = explicit_pronoun

    # If the current English clause clearly has a third-person subject (e.g., "the Levites")
    # but our context is still the congregational "we", switch to that subject unless the
    # Korean source explicitly contained first-person markers.
    if (not _contains_we_markers(source_text)):
        inferred_subj, inferred_pronoun = _infer_subject_from_english(en, "", "")
        if inferred_pronoun and (not pronoun_key or pronoun_key == "we"):
            pronoun_key = inferred_pronoun
            if ctx:
                ctx.pronoun = inferred_pronoun
                if inferred_subj:
                    ctx.subject = inferred_subj

    if not pronoun_key:
        return en
    forms = PRONOUN_FORMS.get(pronoun_key)
    if not forms:
        return en

    updated = en
    replacements = [
        # First-person plural (we → they, etc.) to enforce third-person continuity
        (r"\bwe['’]re\b", forms.get("be_present_contracted") or forms.get("be_present")),
        (r"\bwe are\b", forms.get("be_present")),
        (r"\bwe were\b", forms.get("be_past")),
        (r"\bwe['’]ve\b", forms.get("have_contracted") or forms.get("have")),
        (r"\bwe have\b", forms.get("have")),
        (r"\bwe['’]ll\b", forms.get("will_contracted") or forms.get("will")),
        (r"\bwe will\b", forms.get("will")),
        (r"\bwe['’]d\b", forms.get("would_contracted") or forms.get("would")),
        (r"\bwe would\b", forms.get("would")),
        (r"\bwe can\b", forms.get("can")),
        (r"\bwe\b", forms.get("subject")),
        (r"\bus\b", forms.get("object")),
        (r"\bour\b", forms.get("possessive")),
        (r"\bourselves\b", forms.get("reflexive")),
        (r"\bfor ourselves\b", f"for {forms['reflexive']}"),
        (r"\bby ourselves\b", f"by {forms['reflexive']}"),
        (r"\bon our own\b", f"on {forms['possessive']} own"),
        (r"\bI['’]m\b", forms.get("be_present_contracted") or forms.get("be_present")),
        (r"\bI am\b", forms.get("be_present")),
        (r"\bI was\b", forms.get("be_past")),
        (r"\bI['’]ve\b", forms.get("have_contracted") or forms.get("have")),
        (r"\bI have\b", forms.get("have")),
        (r"\bI['’]ll\b", forms.get("will_contracted") or forms.get("will")),
        (r"\bI will\b", forms.get("will")),
        (r"\bI['’]d\b", forms.get("would_contracted") or forms.get("would")),
        (r"\bI would\b", forms.get("would")),
        (r"\bI can\b", forms.get("can")),
        (r"\bI\b", forms.get("subject")),
        (r"\bme\b", forms.get("object")),
        (r"\bmy\b", forms.get("possessive")),
        (r"\bmyself\b", forms.get("reflexive")),
        (r"\bfor myself\b", f"for {forms['reflexive']}"),
        (r"\bby myself\b", f"by {forms['reflexive']}"),
        (r"\bon my own\b", f"on {forms['possessive']} own"),
    ]
    for pattern, repl in replacements:
        if not repl:
            continue
        updated = re.sub(pattern, lambda m: _format_replacement(m, repl), updated, flags=re.IGNORECASE)
    return updated


def _enforce_we_guardrails(en: str, source_text: str, ctx: Optional[TranslationContext]) -> str:
    """Keep congregational tone in first-person plural when appropriate.

    Trigger if:
      - Korean contains 우리/우리의 markers, OR
      - Context pronoun is set to "we".
    Converts stray third-person pronouns to inclusive "we/our/us" and prefers "let us" invitations.
    """
    if _contains_first_person_markers(source_text) or _contains_implicit_first_person_kinship(source_text):
        return en
    pronoun_pref = _normalize_pronoun(ctx)
    if not _contains_we_markers(source_text) and pronoun_pref != "we":
        return en

    updated = en
    replacements = [
        (r"\bHe\s+encourages\b", "Let us"),
        (r"\bShe\s+encourages\b", "Let us"),
        (r"\bThey\s+encourage\b", "Let us"),
        (r"\bhe\b", "we"),
        (r"\bshe\b", "we"),
        (r"\bthey\b", "we"),
        (r"\bhim\b", "us"),
        (r"\bher\b", "us"),
        (r"\bthem\b", "us"),
        (r"\bhis\b", "our"),
        (r"\bher\b", "our"),
        (r"\btheir\b", "our"),
        (r"\bhe is\b", "we are"),
        (r"\bshe is\b", "we are"),
        (r"\bthey are\b", "we are"),
        (r"\bhe was\b", "we were"),
        (r"\bshe was\b", "we were"),
        (r"\bthey were\b", "we were"),
    ]

    for pattern, repl in replacements:
        updated = re.sub(pattern, lambda m: _format_replacement(m, repl), updated, flags=re.IGNORECASE)

    # Prefer invitation tone
    updated = re.sub(r"\b(he|she|they)\s+should\s+go\b", "let us go", updated, flags=re.IGNORECASE)
    updated = re.sub(r"\b(he|she|they)\s+encourage[s]?\s+\w+\b", "let us", updated, flags=re.IGNORECASE)
    return updated


async def translate_text(
    text: str,
    source: str,
    target: str,
    ctx: Optional[TranslationContext] = None,
    *,
    update_ctx: bool = True,
) -> str:
    """
    Async translator. Returns ONLY the translated text (no quotes/explanations).
    On any API error, it fails open by returning the original text.
    """
    text = (text or "").strip()
    text = _preprocess_source_text(text, source)
    masked_text, hard_map = _mask_hard_glossary(text, source)
    if source.lower().startswith("ko"):
        masked_text = apply_ko_spacing(masked_text)
    if not masked_text:
        return ""

    explicit_first_person = _contains_first_person_markers(text)
    implicit_first_person = _contains_implicit_first_person_kinship(text)

    if ctx:
        # Use the most recent English line to shape the subject/pronoun hint.
        subj_hint, pronoun_hint = _infer_subject_from_english(
            ctx.last_english or "",
            ctx.subject or ENV.CONTEXT_SUBJECT,
            ctx.pronoun or ENV.CONTEXT_PRONOUN,
        )
        ctx.subject = subj_hint
        ctx.pronoun = pronoun_hint

    if explicit_first_person:
        ctx_for_prompt = None
    elif implicit_first_person and ctx:
        ctx_for_prompt = TranslationContext(
            subject="the speaker",
            pronoun="I",
            narration_mode=ctx.narration_mode,
            last_english=ctx.last_english,
        )
    else:
        ctx_for_prompt = ctx

    client = _get_client()
    system = _build_system_prompt(source, target, ctx_for_prompt, current_source_text=text)
    user_content = masked_text
    if ctx_for_prompt:
        prev = ctx_for_prompt.last_english or "(none yet)"
        subject_hint = ctx_for_prompt.subject or ENV.CONTEXT_SUBJECT
        pronoun_hint = ctx_for_prompt.pronoun or ENV.CONTEXT_PRONOUN
        user_content = (
            f"Previous English sentence: {prev}\n"
            f"Subject hint: continue referring to {subject_hint} ({pronoun_hint}).\n\n"
            f"Current text:\n{masked_text}"
        )

    try:
        resp = await client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content}
            ],
            temperature=0.2,
            presence_penalty=0,
            frequency_penalty=0,
            top_p=1.0,
        )
        out = (resp.choices[0].message.content or "").strip()
        out = out.strip('"\u201c\u201d')
        out = _unmask_hard_glossary(out, hard_map)
        if ctx and source.lower().startswith("ko"):
            out = _enforce_subject_guardrails(out, text, ctx)
        if source.lower().startswith("ko"):
            out = _enforce_we_guardrails(out, text, ctx)

        if ctx and update_ctx:
            # Update context for the next clause so subject continuity follows
            # the most recent English output instead of reverting to defaults.
            ctx.subject, ctx.pronoun = _infer_subject_from_english(
                out,
                ctx.subject or ENV.CONTEXT_SUBJECT,
                ctx.pronoun or ENV.CONTEXT_PRONOUN,
            )
            ctx.last_english = out

        _log_translation_example(
            source_lang=source,
            target_lang=target,
            stt_text=text,
            auto_translation=out,
            final_translation=None,
        )

        return out
    except Exception as e:
        print(f"[TX] OpenAI error: {e}")
        return text

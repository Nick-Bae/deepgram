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

# Load Bible names map (Korean -> English) from JSON
_DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data"
_BIBLE_NAMES_PATH = _DATA_DIR / "bible_names.json"
_TRANSLATION_LOG_PATH = _DATA_DIR / "translation_examples.jsonl"
_CUSTOM_PROMPT_PATH = _DATA_DIR / "custom_prompt.txt"

try:
    with open(_BIBLE_NAMES_PATH, encoding="utf-8") as f:
        BIBLE_NAMES: dict[str, str] = json.load(f)
    print(f"[TX] Loaded {len(BIBLE_NAMES)} Bible names from {__file__}")
except FileNotFoundError:
    print(f"[TX] bible_names.json not found at {_BIBLE_NAMES_PATH}; continuing without Bible name map")
BIBLE_NAMES = {}

FIRST_PERSON_KO_MARKERS = [
    "나는", "난", "내가", "내게", "나를", "나도", "나만", "나와", "나에게", "나한테",
    "저는", "전", "제가", "제게", "저를", "저도", "저만", "저와", "저에게", "저한테",
    "우리", "우리가", "우리는", "우릴", "우리의", "우리도", "우리만", "우리와", "우리에게", "우리한테"
]

WE_KO_MARKERS = [
    "우리", "우리가", "우리는", "우릴", "우리의", "우리도", "우리만", "우리와", "우리에게", "우리한테",
]

PRONOUN_FORMS = {
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
    if raw.startswith("he"):
        return "he"
    if raw.startswith("she"):
        return "she"
    if raw.startswith("they"):
        return "they"
    if raw.startswith("we"):
        return "we"
    return None


def _contains_first_person_markers(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return any(marker in compact for marker in FIRST_PERSON_KO_MARKERS)


def _contains_we_markers(text: str) -> bool:
    compact = re.sub(r"\s+", "", text or "")
    return any(marker in compact for marker in WE_KO_MARKERS)


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
        "explicitly contains first-person markers (나/저/우리 variants).\n"
        "- When translating 스스로 or similar reflexives, use himself/herself/themselves matching the subject.\n"
    )


def _build_system_prompt(source: str, target: str, ctx: Optional[TranslationContext], *, current_source_text: Optional[str] = None) -> str:
    """
    Sermon translator with:
    - light STT error correction
    - theological term preferences
    - Biblical names recovery
    - church-safe, cautious language
    """
    source_name = _language_name(source)
    target_name = _language_name(target)

    # Theological glossary (only if from Korean)
    glossary_lines: list[str] = []
    if source_name == "Korean":
        for src, tgt in THEOLOGICAL_TERMS:
            glossary_lines.append(f'- Translate "{src}" as "{tgt}".')

    glossary_block = (
        "\nImportant terminology preferences for key theological words:\n"
        + "\n".join(glossary_lines)
        + "\n"
    ) if glossary_lines else ""

    # Bible names list (Korean -> English)
    bible_name_lines: list[str] = []
    if source_name == "Korean" and BIBLE_NAMES:
        for ko_name, en_name in BIBLE_NAMES.items():
            bible_name_lines.append(f'- "{ko_name}" → "{en_name}"')

    bible_names_block = ""
    if bible_name_lines:
        bible_names_block = (
            "\nCommon Biblical names and places (Korean → standard English form):\n"
            + "\n".join(bible_name_lines)
            + "\n"
        )

    fewshot_block = _build_fewshot_block(source, target, current_source_text=current_source_text)
    custom_prompt = (_get_custom_prompt() or "").strip()
    custom_block = ""
    if custom_prompt:
        custom_block = (
            "\nCustom guidance (set by admins):\n"
            + custom_prompt
            + "\n"
        )

    system = (
        "You are a professional translator specializing in Christian theology and sermons.\n"
        f"Your task is to translate from {source_name} to {target_name}.\n"
        "\n"
        "The source text often comes from automatic speech recognition (STT), "
        "so there may be small recognition errors, especially with similar-sounding Korean words "
        "and Biblical names.\n"
        "If a word or name looks clearly wrong, ungrammatical, or unnatural in context, "
        "silently infer the most likely intended original wording or name and translate that intended meaning.\n"
        "Do NOT mention that you corrected anything; just translate as if the input were already correct.\n"
        + bible_names_block
        + glossary_block
        + fewshot_block
        + custom_block +
        "\n"
        "Context:\n"
        "- The input text is usually a spoken sermon, Bible teaching, or church-related script.\n"
        "- The translation will often be read aloud or shown as subtitles or slides during worship, "
        "including with children and families present.\n"
        "\n"
        "Style and safety rules:\n"
        "1. Use clear, natural, contemporary language that sounds like a respectful, mature pastor.\n"
        "2. Never use slang, jokes, or expressions that could sound flirtatious, crude, or suggestive.\n"
        "3. Never use profanity, vulgar language, or sexual slang under any circumstances.\n"
        "4. When translating phrases about a husband and wife spending time together, "
        "or people enjoying time together (for example, '아내와 나는 오늘밤 좋은 시간을 보내고 있습니다'), "
        "avoid ambiguous phrases like 'having a good time tonight' that could sound romantic or sexual. "
        "Use explicit, wholesome wording that fits a church context, such as "
        "'My wife and I are spending a good evening together, talking and sharing.'\n"
        "5. When the Korean text is emotionally warm but innocent (for example about family, marriage, or friendships), "
        "translate it with gentle, wholesome wording. Do NOT introduce romantic or sexual nuance that is not clearly in the original.\n"
        "6. If a phrase could sound suggestive in English (for example, 'having a good time tonight'), replace it with explicit, "
        "safe, descriptive wording like 'spending a good evening together talking and sharing' or 'enjoying time together as a family.'\n"
        "7. Do not exaggerate or dramatize emotions beyond what the Korean clearly expresses; keep the tone steady and pastoral.\n"
        "8. When the Korean is general or ambiguous, keep a similar level of generality in the translation and do not add specific details.\n"
        "9. Always choose wording that is completely appropriate to say in a mixed-age worship service with children, teens, and adults.\n"
        "10. Do not introduce people or place names (e.g., Bible figures) unless that name is explicitly present in the Korean source clause.\n"
        "Spacing reliability: Korean STT often omits spaces; mentally restore natural spacing before translating so words are not merged or dropped.\n"
        "Congregation cues: If the Korean clause uses 일어나/일어나서/일어나셔서/자리에서 일어나 without an explicit subject, "
        "assume the speaker is inviting the congregation. Translate with an invitation like 'let's stand' or 'please stand' "
        "instead of 'he stood up.' Preserve timing phrases such as '이 시간' (e.g., '이 시간 다 같이 일어나셔서' → 'At this time, let's stand together').\n"
        "\n"
        "General requirements:\n"
        "11. Preserve biblical and theological meaning very accurately.\n"
        "12. Break up very long sentences into shorter, easy-to-follow sentences suitable for listening.\n"
        "13. Keep all Scripture references, person names, and place names correct (using the Bible name list above when relevant).\n"
        "14. Preserve paragraph and line-break structure as much as reasonably possible.\n"
        "15. Perform only light, obvious corrections to STT mistakes; do not rewrite or summarize.\n"
        "16. Do not add explanations, comments, headings, or brackets.\n"
        "17. Output ONLY the translated text; no quotes, no extra commentary, no meta text.\n"
        "18. If the input seems incomplete or ends abruptly (common with STT pauses), translate only what was actually said and do not invent endings or extra sentences.\n"
    )

    return system + _build_context_block(ctx)


def _enforce_subject_guardrails(en: str, source_text: str, ctx: Optional[TranslationContext]) -> str:
    pronoun_key = _normalize_pronoun(ctx)
    if not pronoun_key:
        return en
    forms = PRONOUN_FORMS.get(pronoun_key)
    if not forms:
        return en
    if _contains_first_person_markers(source_text):
        return en

    updated = en
    replacements = [
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


async def translate_text(text: str, source: str, target: str, ctx: Optional[TranslationContext] = None) -> str:
    """
    Async translator. Returns ONLY the translated text (no quotes/explanations).
    On any API error, it fails open by returning the original text.
    """
    text = (text or "").strip()
    text = _preprocess_source_text(text, source)
    if source.lower().startswith("ko"):
        text = apply_ko_spacing(text)
    if not text:
        return ""

    explicit_first_person = _contains_first_person_markers(text)
    ctx_for_prompt = None if explicit_first_person else ctx

    client = _get_client()
    system = _build_system_prompt(source, target, ctx_for_prompt, current_source_text=text)
    user_content = text
    if ctx_for_prompt:
        prev = ctx_for_prompt.last_english or "(none yet)"
        subject_hint = ctx_for_prompt.subject or ENV.CONTEXT_SUBJECT
        pronoun_hint = ctx_for_prompt.pronoun or ENV.CONTEXT_PRONOUN
        user_content = (
            f"Previous English sentence: {prev}\n"
            f"Subject hint: continue referring to {subject_hint} ({pronoun_hint}).\n\n"
            f"Current text:\n{text}"
        )

    try:
        resp = await client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_content}
            ],
            temperature=0.2,
        )
        out = (resp.choices[0].message.content or "").strip()
        out = out.strip('"\u201c\u201d')
        if ctx and source.lower().startswith("ko"):
            out = _enforce_subject_guardrails(out, text, ctx)
        if source.lower().startswith("ko"):
            out = _enforce_we_guardrails(out, text, ctx)

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

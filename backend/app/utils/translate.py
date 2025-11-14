# backend/app/utils/translate.py
import os
import json
import pathlib
from datetime import datetime
from typing import Iterable, List, Optional
from collections import deque
from dotenv import load_dotenv

# OpenAI >= 1.0
from openai import AsyncOpenAI

load_dotenv()

_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # fast & good; use gpt-4o if you want
_API_KEY = os.getenv("OPENAI_API_KEY")

_client: AsyncOpenAI | None = None

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

try:
    with open(_BIBLE_NAMES_PATH, encoding="utf-8") as f:
        BIBLE_NAMES: dict[str, str] = json.load(f)
    print(f"[TX] Loaded {len(BIBLE_NAMES)} Bible names from {__file__}")
except FileNotFoundError:
    print(f"[TX] bible_names.json not found at {_BIBLE_NAMES_PATH}; continuing without Bible name map")
    BIBLE_NAMES = {}


def _ensure_data_dir() -> None:
    try:
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(f"[TX] failed to ensure data dir {_DATA_DIR}: {exc}")


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


def _load_fewshot_examples(
    source_lang: str,
    target_lang: str,
    *,
    max_examples: int = 4,
) -> List[dict[str, str]]:
    if not _TRANSLATION_LOG_PATH.exists() or max_examples <= 0:
        return []

    corrected: deque[dict[str, str]] = deque(maxlen=max_examples)
    fallback: deque[dict[str, str]] = deque(maxlen=max_examples)

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

                source_text = (record.get("stt_text") or "").strip()
                final_text = (record.get("final_translation") or record.get("auto_translation") or "").strip()
                if not source_text or not final_text:
                    continue

                pair = {"source": source_text, "target": final_text}
                if record.get("corrected"):
                    corrected.append(pair)
                else:
                    fallback.append(pair)
    except Exception as exc:
        print(f"[TX] Failed to read translation examples: {exc}")
        return []

    examples = list(corrected)
    if len(examples) < max_examples:
        needed = max_examples - len(examples)
        examples.extend(list(fallback)[-needed:])

    return examples[-max_examples:]


def _build_fewshot_block(source: str, target: str) -> str:
    examples = _load_fewshot_examples(source, target, max_examples=4)
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
        "\nHere are recent corrections that show the desired style:\n\n"
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


def _build_system_prompt(source: str, target: str) -> str:
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

    fewshot_block = _build_fewshot_block(source, target)

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
        + fewshot_block +
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
        "\n"
        "General requirements:\n"
        "10. Preserve biblical and theological meaning very accurately.\n"
        "11. Break up very long sentences into shorter, easy-to-follow sentences suitable for listening.\n"
        "12. Keep all Scripture references, person names, and place names correct (using the Bible name list above when relevant).\n"
        "13. Preserve paragraph and line-break structure as much as reasonably possible.\n"
        "14. Perform only light, obvious corrections to STT mistakes; do not rewrite or summarize.\n"
        "15. Do not add explanations, comments, headings, or brackets.\n"
        "16. Output ONLY the translated text; no quotes, no extra commentary, no meta text.\n"
    )

    return system


async def translate_text(text: str, source: str, target: str) -> str:
    """
    Async translator. Returns ONLY the translated text (no quotes/explanations).
    On any API error, it fails open by returning the original text.
    """
    text = (text or "").strip()
    if not text:
        return ""

    client = _get_client()
    system = _build_system_prompt(source, target)

    try:
        resp = await client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text}
            ],
            temperature=0.2,
        )
        out = (resp.choices[0].message.content or "").strip()
        out = out.strip('"\u201c\u201d')

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

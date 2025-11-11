# backend/app/utils/translate.py
import os
import json
import pathlib
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

try:
    with open(_BIBLE_NAMES_PATH, encoding="utf-8") as f:
        BIBLE_NAMES: dict[str, str] = json.load(f)
    print(f"[TX] Loaded {len(BIBLE_NAMES)} Bible names from {__file__}")
except FileNotFoundError:
    print(f"[TX] bible_names.json not found at {_BIBLE_NAMES_PATH}; continuing without Bible name map")
    BIBLE_NAMES = {}


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
        + glossary_block +
        "\n"
        "Context:\n"
        "- The input text is usually a spoken sermon, Bible teaching, or church-related script.\n"
        "- The translation will often be read aloud or shown as subtitles or slides during worship.\n"
        "\n"
        "Requirements:\n"
        "1. Preserve biblical and theological meaning very accurately.\n"
        f"2. Use clear, natural, contemporary {target_name} that sounds like a native-speaking pastor.\n"
        "3. Break up very long sentences into shorter, easy-to-follow sentences suitable for listening.\n"
        "4. Keep all Scripture references, person names, and place names correct (using the Bible name list above when relevant).\n"
        "5. Preserve paragraph and line-break structure as much as reasonably possible.\n"
        "6. Perform only light, obvious corrections to STT mistakes; do not rewrite or summarize.\n"
        "7. Do not add explanations, comments, headings, or brackets.\n"
        "8. Output ONLY the translated text; no quotes, no extra commentary, no meta text.\n"
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
        # strip any accidental leading/trailing quotes
        return out.strip('"\u201c\u201d')
    except Exception as e:
        print(f"[TX] OpenAI error: {e}")
        return text

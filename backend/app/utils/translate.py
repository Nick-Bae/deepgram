# backend/app/utils/translate.py
import os
from dotenv import load_dotenv

# OpenAI >= 1.0
from openai import AsyncOpenAI

load_dotenv()

_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")  # fast & good; use gpt-4o if you want
_API_KEY = os.getenv("OPENAI_API_KEY")

_client: AsyncOpenAI | None = None

def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        if not _API_KEY:
            raise RuntimeError("OPENAI_API_KEY not set")
        _client = AsyncOpenAI(api_key=_API_KEY)
    return _client

async def translate_text(text: str, source: str, target: str) -> str:
    """
    Async translator. Returns ONLY the translated text (no quotes/explanations).
    On any API error, it fails open by returning the original text.
    """
    text = (text or "").strip()
    if not text:
        return ""

    client = _get_client()

    system = (
        f"You are a professional simultaneous interpreter. "
        f"Translate faithfully from {source} to {target}. "
        f"Do not explain, do not add quotes, do not add brackets—"
        f"output ONLY the translation text."
    )

    try:
        resp = await client.chat.completions.create(
            model=_MODEL,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": text},
            ],
            temperature=0.2,
        )
        out = (resp.choices[0].message.content or "").strip()
        # strip any accidental quotes
        return out.strip('"\u201c\u201d')
    except Exception as e:
        # Log and fail open (so the pipeline keeps moving)
        print(f"[TX] OpenAI error: {e}")
        return text

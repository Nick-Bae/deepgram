from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROMPT_PATH = DATA_DIR / "custom_prompt.txt"

router = APIRouter(prefix="/prompt", tags=["prompt"])


class PromptPayload(BaseModel):
    prompt: str


def _read_prompt() -> str:
    if not PROMPT_PATH.exists():
        return ""
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return ""


@router.get("")
def get_prompt():
    return {"prompt": _read_prompt()}


@router.post("")
def set_prompt(payload: PromptPayload):
    text = payload.prompt or ""
    PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_PATH.write_text(text, encoding="utf-8")
    return {"saved": True, "length": len(text)}


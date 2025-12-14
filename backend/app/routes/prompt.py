from pathlib import Path

from fastapi import APIRouter
from pydantic import BaseModel

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PROMPT_PATH = DATA_DIR / "custom_prompt.txt"
SERVICE_PROMPT_PATH = DATA_DIR / "service_prompt.txt"

router = APIRouter(prefix="/prompt", tags=["prompt"])


class PromptPayload(BaseModel):
    prompt: str = ""
    service_prompt: str = ""


def _read_text(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except Exception:
        return ""


@router.get("")
def get_prompt():
    return {
        "prompt": _read_text(PROMPT_PATH),
        "service_prompt": _read_text(SERVICE_PROMPT_PATH),
    }


@router.post("")
def set_prompt(payload: PromptPayload):
    text = payload.prompt or ""
    service_text = payload.service_prompt or ""
    PROMPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    PROMPT_PATH.write_text(text, encoding="utf-8")
    SERVICE_PROMPT_PATH.write_text(service_text, encoding="utf-8")
    return {
        "saved": True,
        "length": len(text),
        "service_length": len(service_text),
    }

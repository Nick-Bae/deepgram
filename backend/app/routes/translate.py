from fastapi import APIRouter, HTTPException
from app.utils.translate import translate_text
from app.socket_manager import manager
import inspect
import logging
from time import perf_counter

logger = logging.getLogger("app.translate")

router = APIRouter()

async def _resolve(maybe_awaitable):
    return await maybe_awaitable if inspect.isawaitable(maybe_awaitable) else maybe_awaitable

@router.post("/translate")
async def translate(data: dict):
    text = (data.get("text") or "").trim() if hasattr(str, "trim") else (data.get("text") or "").strip()
    source = (data.get("source") or "ko").strip()
    target = (data.get("target") or "en").strip()
    is_final = bool(data.get("final", False))

    if not text:
        raise HTTPException(status_code=400, detail="No text provided for translation")

    t0 = perf_counter()
    try:
        translated = await _resolve(translate_text(text, source, target))
    except Exception as e:
        logger.exception("translate_text raised")
        raise HTTPException(status_code=500, detail=f"translator_error: {e}")

    dt_ms = int((perf_counter() - t0) * 1000)

    if not isinstance(translated, str):
        logger.error("translator returned non-string", extra={"type": type(translated).__name__})
        raise HTTPException(status_code=500, detail="Translator returned non-string result")

    if translated.strip() == "":
        logger.error("empty translation result")
        raise HTTPException(status_code=500, detail="Empty translation result")

    # Log IN -> OUT
    logger.info(
        "translate",
        extra={
            "source": source,
            "target": target,
            "final": is_final,
            "ms": dt_ms,
            "in": text[:200],
            "out": translated[:200],
        },
    )

    await manager.broadcast({
        "type": "translation",
        "payload": translated,
        "lang": target,
        "meta": {"is_final": is_final, "source": source}
    })

    return {"translated": translated, "final": is_final}

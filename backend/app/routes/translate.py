import inspect
import io
import logging
from time import perf_counter

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.services.google_tts import synthesize_async as synthesize_google_tts
from app.services.gemini_flash_tts import synthesize_async as synthesize_gemini_tts
from app.socket_manager import manager
from app.utils.translate import translate_text

logger = logging.getLogger("app.translate")

router = APIRouter()

async def _resolve(maybe_awaitable):
    return await maybe_awaitable if inspect.isawaitable(maybe_awaitable) else maybe_awaitable


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=2000)
    lang: str | None = Field(default="en")
    voice: str | None = Field(default=None)
    speaking_rate: float | None = Field(default=None, ge=0.25, le=4.0)
    pitch: float | None = Field(default=None, ge=-20.0, le=20.0)
    provider: str | None = Field(default="google", description="google | gemini_flash")

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


@router.post("/tts")
async def synthesize_tts(payload: TTSRequest):
    provider = (payload.provider or "google").strip().lower()
    if provider in {"google", "google_cloud", "gcp"}:
        synthesize = synthesize_google_tts
    elif provider in {"gemini_flash", "gemini", "gemini_flash_tts"}:
        synthesize = synthesize_gemini_tts
    else:
        raise HTTPException(status_code=400, detail=f"Unsupported TTS provider: {provider}")

    try:
        audio_bytes, meta = await synthesize(
            payload.text,
            language=payload.lang,
            voice=payload.voice,
            speaking_rate=payload.speaking_rate,
            pitch=payload.pitch,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:  # pragma: no cover - log + hide details from client
        logger.exception("tts synth failed")
        raise HTTPException(status_code=502, detail="tts_failed") from exc

    headers = {
        "Cache-Control": "no-store",
        "X-TTS-Voice": meta.get("voice_name", ""),
        "X-TTS-Language": meta.get("language_code", ""),
        "X-TTS-Provider": provider,
        "Content-Length": str(len(audio_bytes)),
    }
    return StreamingResponse(io.BytesIO(audio_bytes), media_type="audio/mpeg", headers=headers)

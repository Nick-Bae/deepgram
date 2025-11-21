from __future__ import annotations
import base64, json, time, asyncio
from typing import Any
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from .env import ENV
from .chunker.ko_chunker import KoChunker
from asyncio import Lock
from .translator.openai_translator import translate_ko_to_en_chunk, TranslationContext
from .asr_bridge import AsrBridge

router = APIRouter()

@router.websocket("/ws")
async def ws_handler(ws: WebSocket):
    await ws.accept()
    bridge = AsrBridge()  # replace with your concrete adapter if you have one
    chunker = KoChunker()
    t_start = int(time.time() * 1000)
    next_id = 1
    context = TranslationContext()
    context_lock = Lock()

    async def _safe_send(payload: dict[str, Any]):
        if ws.application_state == WebSocketState.CONNECTED:
            await ws.send_text(json.dumps(payload, ensure_ascii=False))

    async def commit_now(ko: str, t_audio_end: int):
        nonlocal next_id
        async with context_lock:
            en = await translate_ko_to_en_chunk(ko, context)
            context.last_english = en
        emit_ts = int(time.time() * 1000)
        await _safe_send({"type":"commit","commit":{
            "id": str(next_id), "ko": ko, "en": en,
            "tAudioEnd": t_audio_end, "tEmit": emit_ts
        }})
        next_id += 1
        await _safe_send({"type":"metrics","lagMs": emit_ts - (t_start + t_audio_end)})

    def on_partial(p):
        now = int(time.time() * 1000)
        for ko in chunker.push_partial(p.text, now):
            asyncio.create_task(commit_now(ko, p.t1_ms or (now - t_start)))
        if p.is_final:
            for ko in chunker.finalize(now):
                asyncio.create_task(commit_now(ko, p.t1_ms or (now - t_start)))

    bridge.on_partial(on_partial)

    try:
        while True:
            msg = await ws.receive_text()
            data = json.loads(msg)
            if data.get("type") == "audio":
                b64 = data.get("pcm16"); sr = int(data.get("sampleRate", 16000))
                if b64: bridge.push_pcm16(base64.b64decode(b64), sr)
            elif data.get("type") == "mock":
                bridge.mock_feed()
            elif data.get("type") == "stop":
                break
    except WebSocketDisconnect:
        pass

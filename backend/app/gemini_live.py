from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode


GEMINI_LIVE_TRANSLATE_MODEL = os.getenv(
    "GEMINI_LIVE_TRANSLATE_MODEL",
    "gemini-3.5-live-translate-preview",
)
GEMINI_LIVE_TRANSLATE_ENDPOINT = os.getenv(
    "GEMINI_LIVE_TRANSLATE_ENDPOINT",
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent",
)
GEMINI_INPUT_SAMPLE_RATE = 16_000
GEMINI_OUTPUT_SAMPLE_RATE = 24_000
GEMINI_INPUT_CHUNK_BYTES = 3_200  # 100 ms of mono PCM16 at 16 kHz.


def api_key() -> str:
    return (os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or "").strip()


def websocket_url(key: str) -> str:
    return f"{GEMINI_LIVE_TRANSLATE_ENDPOINT}?{urlencode({'key': key})}"


def target_language_code(raw: str | None) -> str:
    token = (raw or "en").strip().lower()
    aliases = {
        "zh": "zh-Hans",
        "zh-cn": "zh-Hans",
        "zh-sg": "zh-Hans",
        "zh-hans": "zh-Hans",
        "zh-tw": "zh-Hant",
        "zh-hk": "zh-Hant",
        "zh-hant": "zh-Hant",
        "pt-br": "pt-BR",
        "pt-pt": "pt-PT",
        "en-us": "en",
        "en-gb": "en",
        "ko-kr": "ko",
        "es-es": "es",
        "es-us": "es",
    }
    return aliases.get(token, token.split("-")[0] or "en")


def setup_message(target_language: str) -> str:
    return json.dumps(
        {
            "setup": {
                "model": f"models/{GEMINI_LIVE_TRANSLATE_MODEL}",
                # These are BidiGenerateContentSetup fields in the wire schema,
                # not GenerationConfig fields.
                "inputAudioTranscription": {},
                "outputAudioTranscription": {},
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "translationConfig": {
                        "targetLanguageCode": target_language,
                        "echoTargetLanguage": True,
                    },
                },
            }
        }
    )


def audio_message(pcm16: bytes) -> str:
    return json.dumps(
        {
            "realtimeInput": {
                "audio": {
                    "data": base64.b64encode(pcm16).decode("ascii"),
                    "mimeType": f"audio/pcm;rate={GEMINI_INPUT_SAMPLE_RATE}",
                }
            }
        }
    )


class Pcm16Downsampler48To16:
    """Stateful exact-ratio PCM16 downsampler for browser 48 kHz chunks."""

    def __init__(self) -> None:
        self._pending = b""

    def push(self, pcm48: bytes) -> bytes:
        data = self._pending + (pcm48 or b"")
        complete_groups = (len(data) // 6) * 6
        if complete_groups <= 0:
            self._pending = data
            return b""

        body = data[:complete_groups]
        self._pending = data[complete_groups:]
        output = bytearray(complete_groups // 3)
        write_at = 0
        for read_at in range(0, complete_groups, 6):
            output[write_at:write_at + 2] = body[read_at:read_at + 2]
            write_at += 2
        return bytes(output)


class PcmChunkBuffer:
    def __init__(self, chunk_bytes: int = GEMINI_INPUT_CHUNK_BYTES) -> None:
        self._chunk_bytes = chunk_bytes
        self._pending = bytearray()

    def push(self, pcm: bytes) -> list[bytes]:
        if pcm:
            self._pending.extend(pcm)
        chunks: list[bytes] = []
        while len(self._pending) >= self._chunk_bytes:
            chunks.append(bytes(self._pending[:self._chunk_bytes]))
            del self._pending[:self._chunk_bytes]
        return chunks

    def flush(self) -> bytes:
        remaining = bytes(self._pending)
        self._pending.clear()
        return remaining


def merge_transcript(current: str, fragment: str) -> str:
    left = current or ""
    right = fragment or ""
    if not right:
        return left
    if not left:
        return right
    if right.startswith(left):
        return right
    if left.endswith(right):
        return left

    max_overlap = min(len(left), len(right), 160)
    for size in range(max_overlap, 0, -1):
        if left[-size:] == right[:size]:
            return left + right[size:]
    return left + right


@dataclass(frozen=True)
class GeminiServerContent:
    input_transcript: str
    output_transcript: str
    audio_chunks: tuple[str, ...]
    turn_complete: bool
    interrupted: bool


def parse_server_content(event: dict[str, Any]) -> GeminiServerContent:
    content = event.get("serverContent")
    if not isinstance(content, dict):
        return GeminiServerContent("", "", (), False, False)

    input_tx = content.get("inputTranscription")
    output_tx = content.get("outputTranscription")
    input_text = str(input_tx.get("text") or "") if isinstance(input_tx, dict) else ""
    output_text = str(output_tx.get("text") or "") if isinstance(output_tx, dict) else ""

    audio_chunks: list[str] = []
    model_turn = content.get("modelTurn")
    parts = model_turn.get("parts") if isinstance(model_turn, dict) else None
    if isinstance(parts, list):
        for part in parts:
            if not isinstance(part, dict):
                continue
            inline = part.get("inlineData")
            if not isinstance(inline, dict):
                continue
            data = inline.get("data")
            if isinstance(data, str) and data:
                audio_chunks.append(data)

    return GeminiServerContent(
        input_transcript=input_text,
        output_transcript=output_text,
        audio_chunks=tuple(audio_chunks),
        turn_complete=bool(content.get("turnComplete")),
        interrupted=bool(content.get("interrupted")),
    )

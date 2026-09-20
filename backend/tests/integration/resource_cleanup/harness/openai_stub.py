"""Minimal OpenAI-compatible HTTP server for the resource-cleanup
harness. The backend's translator (`app/utils/translate.py`) uses
`AsyncOpenAI(api_key=...)` and, absent an explicit `base_url`, reads
`OPENAI_BASE_URL` from the environment. Point that env var at this
stub's URL and the backend's translation calls hit us instead of
`api.openai.com` — no external network, no paid credentials.

Behaviour: pass-through with a fixed suffix. Given a user message
`"안녕하세요 <marker>"` the stub returns `"[stub] <marker>"` as the
translation. Callers assert the marker survives the round trip through
Deepgram stub → backend → OpenAI stub → broadcast → listener, which
proves the real translation and broadcast paths executed."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
from typing import Optional

try:
    from aiohttp import web
except ImportError:  # pragma: no cover
    # aiohttp is present in requirements-dev.txt; keep an explicit
    # message so a stripped venv fails clearly.
    raise RuntimeError(
        "aiohttp is required for the OpenAI stub — "
        "install via `pip install aiohttp`"
    )


# Extract a unique-looking marker token from the transcript so the
# test can assert it survived the pipeline. Marker shape is any
# lowercase-with-dashes prefix followed by a hex tail — the specific
# prefix identifies which test emitted the marker (baseline-,
# cross-process-, post-sigterm-, isolation-, cross-, handover-…),
# but the stub does not need an allowlist. A prefix allowlist would
# silently drop tests that use a new marker name (e.g. F-25's
# `isolation-*` and F-26's `cross-*` were both dropped by an earlier
# regex, making their assertions impossible to satisfy).
_MARKER_RE = re.compile(
    r"[a-z][a-z-]*-[a-f0-9]{4,}"
)


def _extract_marker(text: str) -> str:
    m = _MARKER_RE.search(text or "")
    if not m:
        return ""
    return m.group(0)


def _translate(text: str) -> str:
    marker = _extract_marker(text)
    if marker:
        return f"[stub-translated] {marker}"
    return "[stub-translated]"


class OpenAIStub:
    """HTTP server that speaks enough of the OpenAI chat-completions
    API for the translator. Bind to 127.0.0.1:0 (any free port);
    expose `.base_url` for `OPENAI_BASE_URL`."""

    def __init__(self):
        self._runner: Optional[web.AppRunner] = None
        self._site: Optional[web.TCPSite] = None
        self._port: int = 0
        self.request_count = 0

    @property
    def port(self) -> int:
        return self._port

    @property
    def base_url(self) -> str:
        # OpenAI SDK expects the base_url to end in /v1 (it appends
        # `/chat/completions` and other paths itself).
        return f"http://127.0.0.1:{self._port}/v1"

    async def start(self) -> None:
        app = web.Application()
        app.router.add_post("/v1/chat/completions", self._handle_chat_completions)
        # Some SDK versions probe /models on init — handle gracefully.
        app.router.add_get("/v1/models", self._handle_models)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        self._site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        for server in getattr(self._site._server, "sockets", []) or []:
            self._port = server.getsockname()[1]
            break
        if not self._port:
            # aiohttp sometimes stashes the port on the site object
            # differently across versions; fall back to the runner's
            # sockets.
            for sock in getattr(self._runner, "sites", []):
                addr = getattr(sock, "_server", None)
                if addr and addr.sockets:
                    self._port = addr.sockets[0].getsockname()[1]
                    break
        if not self._port:
            raise RuntimeError("OpenAI stub failed to bind a port")

    async def stop(self) -> None:
        if self._site is not None:
            with contextlib.suppress(Exception):
                await self._site.stop()
            self._site = None
        if self._runner is not None:
            with contextlib.suppress(Exception):
                await self._runner.cleanup()
            self._runner = None

    async def _handle_models(self, request: web.Request) -> web.Response:
        return web.json_response({
            "object": "list",
            "data": [{"id": "gpt-4o", "object": "model"}],
        })

    async def _handle_chat_completions(self, request: web.Request) -> web.Response:
        self.request_count += 1
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "invalid JSON"}, status=400)

        # Pull the user message text (last user turn).
        messages = body.get("messages") or []
        user_text = ""
        for msg in messages:
            if msg.get("role") == "user":
                content = msg.get("content")
                if isinstance(content, str):
                    user_text = content
                elif isinstance(content, list):
                    parts = []
                    for part in content:
                        if isinstance(part, dict):
                            parts.append(part.get("text") or "")
                    user_text = " ".join(parts)

        translated = _translate(user_text)
        stream = bool(body.get("stream"))
        model = body.get("model") or "gpt-4o"
        completion_id = f"chatcmpl-stub-{self.request_count}"

        if stream:
            # SSE stream. One content chunk + a [DONE] terminator.
            resp = web.StreamResponse(
                status=200,
                headers={"Content-Type": "text/event-stream"},
            )
            await resp.prepare(request)
            chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {"role": "assistant", "content": translated},
                    "finish_reason": None,
                }],
            }
            await resp.write(f"data: {json.dumps(chunk)}\n\n".encode())
            done_chunk = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "model": model,
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }],
            }
            await resp.write(f"data: {json.dumps(done_chunk)}\n\n".encode())
            await resp.write(b"data: [DONE]\n\n")
            await resp.write_eof()
            return resp

        return web.json_response({
            "id": completion_id,
            "object": "chat.completion",
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": translated},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        })

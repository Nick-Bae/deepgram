from __future__ import annotations

import asyncio
import base64
import json
import os
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional
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


# Default is OFF because compatibility of `contextWindowCompression` with
# `gemini-3.5-live-translate-preview` is NOT established in material
# accessible to this session. An unsupported setup field could break
# initial connections. Operators who have confirmed compatibility against
# the specific model may opt in with GEMINI_LIVE_CONTEXT_COMPRESSION=1.
#
# UNRESOLVED: Google's general Live API docs describe ~10-min connection +
# ~15-min audio-session caps for models that support it; the preview
# translation model's effective caps have not been documented in material
# we have access to. Long-session behavior beyond one rotation remains
# unverified and must be measured in staging before any dependency on
# compression (or its absence) is implied.
_CONTEXT_COMPRESSION_DEFAULT = os.getenv(
    "GEMINI_LIVE_CONTEXT_COMPRESSION", "0"
).strip() in {"1", "true", "True"}


def setup_message(
    target_language: str,
    resume_handle: Optional[str] = None,
    *,
    context_compression: Optional[bool] = None,
) -> str:
    """Build a BidiGenerateContentSetup JSON frame.

    `sessionResumption` is a top-level setup field per the Live API wire
    schema. `{}` opts in to resumption and lets the server mint `newHandle`s.
    `{"handle": "..."}` continues an existing session after a GoAway. We do
    NOT log handles.

    `contextWindowCompression.slidingWindow` is OMITTED by default (see
    `_CONTEXT_COMPRESSION_DEFAULT`): compatibility with
    `gemini-3.5-live-translate-preview` is NOT authoritatively documented
    in material accessible here, and shipping an unsupported setup field
    could break initial connections. Opt in with `context_compression=True`
    (or env `GEMINI_LIVE_CONTEXT_COMPRESSION=1`) only after confirming
    model compatibility. When included, the compression field is at the
    TOP of `setup`, not inside `generationConfig`, per the Live API schema.
    """
    session_resumption: dict[str, Any] = {}
    if resume_handle:
        session_resumption = {"handle": resume_handle}
    use_compression = (
        _CONTEXT_COMPRESSION_DEFAULT if context_compression is None else context_compression
    )
    setup: dict[str, Any] = {
        "model": f"models/{GEMINI_LIVE_TRANSLATE_MODEL}",
        "sessionResumption": session_resumption,
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
    if use_compression:
        setup["contextWindowCompression"] = {"slidingWindow": {}}
    return json.dumps({"setup": setup})


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


@dataclass(frozen=True)
class GeminiGoAway:
    """Top-level `goAway` event. The server is about to close the connection.

    `time_left_ms` is advisory; may be 0/None if the server omits it.
    """
    time_left_ms: Optional[int]


@dataclass(frozen=True)
class GeminiResumptionUpdate:
    """Top-level `sessionResumptionUpdate` event.

    `resumable` is False → the server does NOT guarantee a lossless resume
    even if `new_handle` is present (and will usually omit it). The client
    MUST NOT substitute an older checkpoint; it would silently diverge.
    """
    new_handle: Optional[str]
    resumable: bool


@dataclass(frozen=True)
class GeminiControlEvent:
    """Dispatched classification of a parsed server event.

    `kind` is one of:
        "setup_complete" (payload=None)
        "content"        (payload=GeminiServerContent)
        "goaway"         (payload=GeminiGoAway)
        "resumption"     (payload=GeminiResumptionUpdate)
        "error"          (payload=dict with "message" key)
        "other"          (payload=raw event dict)
    """
    kind: str
    payload: Any


def parse_control_event(event: dict[str, Any]) -> GeminiControlEvent:
    """Classify a top-level server event. Does not mutate `event`.

    Preserves `parse_server_content` behavior for serverContent events.
    """
    if not isinstance(event, dict):
        return GeminiControlEvent("other", event)

    if event.get("error"):
        err = event["error"]
        if isinstance(err, dict):
            return GeminiControlEvent("error", {"message": err.get("message") or ""})
        return GeminiControlEvent("error", {"message": str(err)})

    if event.get("setupComplete") is not None:
        return GeminiControlEvent("setup_complete", None)

    if "goAway" in event:
        ga = event.get("goAway") or {}
        time_left_ms: Optional[int] = None
        if isinstance(ga, dict):
            # Live API encodes `timeLeft` as a protobuf Duration. The JSON
            # representation is a seconds string with an `s` suffix,
            # possibly fractional, e.g. "60s", "0.250s", "0s". Reference:
            # https://ai.google.dev/api/live#GoAway  +  protobuf Duration.
            # Numeric values are NOT part of the documented contract and we
            # do not guess their meaning — parsing yields None.
            raw = ga.get("timeLeft")
            if isinstance(raw, str):
                s = raw.strip()
                if s.endswith("s") and not s.endswith("ms"):
                    try:
                        time_left_ms = int(float(s[:-1]) * 1000)
                    except Exception:
                        time_left_ms = None
                # Malformed (no 's' suffix, "ms"-suffixed, empty, non-numeric
                # body): time_left_ms stays None (advisory field; we always
                # treat GoAway itself as the actionable signal).
        return GeminiControlEvent("goaway", GeminiGoAway(time_left_ms=time_left_ms))

    if "sessionResumptionUpdate" in event:
        upd = event.get("sessionResumptionUpdate") or {}
        new_handle: Optional[str] = None
        resumable = False
        if isinstance(upd, dict):
            raw_handle = upd.get("newHandle")
            if isinstance(raw_handle, str) and raw_handle:
                new_handle = raw_handle
            resumable = bool(upd.get("resumable"))
        return GeminiControlEvent(
            "resumption",
            GeminiResumptionUpdate(new_handle=new_handle, resumable=resumable),
        )

    if "serverContent" in event:
        return GeminiControlEvent("content", parse_server_content(event))

    return GeminiControlEvent("other", event)


# ---------------------------------------------------------------------------
# Session recovery coordinator
# ---------------------------------------------------------------------------


class GeminiReplacementError(RuntimeError):
    """Raised when replacement-WS setup fails. Caller must surface this as a
    meaningful error — NOT swallow it as a routine timeout."""


class GeminiLiveSession:
    """Coordinates a Gemini Live BidiGenerateContent connection with
    bounded, non-overlapping replacement on `goAway` using the latest
    resumable handle.

    Public coroutines:
        open()                 — open initial WS and complete setup.
        send(text_json)        — send one JSON frame on the current WS.
        recv()                 — receive one raw frame on the current WS.
        rotate(reason)         — explicit rotation (idempotent while running).
        close()                — close current WS and cancel any pending
                                 replacement; after this, send/recv raise.
        current_ws             — introspection only; do not retain.
        latest_handle          — last stored handle (never None after one
                                 resumable=True update); not exposed through
                                 logs by this module.

    `connect_cb` and `setup_cb` are injectable for testability; production
    code passes real connectors.
    """

    def __init__(
        self,
        *,
        target_language: str,
        connect_cb: Callable[[], Awaitable[Any]],
        setup_payload_cb: Optional[Callable[[Optional[str]], str]] = None,
        setup_timeout_s: float = 20.0,
        replacement_total_timeout_s: float = 25.0,
        close_grace_s: float = 3.0,
    ) -> None:
        self._target_language = target_language
        self._connect = connect_cb
        self._build_setup = setup_payload_cb or (
            lambda h: setup_message(target_language, resume_handle=h)
        )
        self._setup_timeout = setup_timeout_s
        self._replacement_total_timeout = replacement_total_timeout_s
        self._close_grace = close_grace_s

        self._ws: Any = None
        # `_rotation_in_flight` is True from the moment rotate() starts
        # opening a replacement until the swap succeeds or the attempt
        # errors/returns. send() uses this (together with `_goaway_observed`
        # and `_closed`) to classify send-failures as benign drops rather
        # than terminal errors, preserving no-blind-replay while letting a
        # planned rotation complete without killing the audio input task.
        self._rotation_in_flight: bool = False
        self._latest_handle: Optional[str] = None
        # `_resumable_state` tracks the resumability of the SERVER's current
        # session, driven by the latest sessionResumptionUpdate:
        #   True  → last update had resumable=True (safe to recover with the
        #           stored handle; the handle is the server's checkpoint).
        #   False → last update had resumable=False (per
        #           https://ai.google.dev/api/live#SessionResumptionUpdate,
        #           reusing an earlier token here can lose data). We treat
        #           the current session as NON-recoverable until a fresh
        #           resumable=True update arrives before the GoAway deadline.
        #   None  → no update seen yet; also non-recoverable (no handle).
        self._resumable_state: Optional[bool] = None
        self._goaway_observed: bool = False
        self._lock = asyncio.Lock()
        # The actively-running replacement task, if any. close() cancels
        # it and awaits its cleanup to ensure no leaked task or WS.
        self._replacement_task: Optional[asyncio.Task[Any]] = None
        # The replacement WS currently being set up (before swap). close()
        # closes this directly so a setup-blocked recv can unblock.
        self._pending_replacement_ws: Any = None
        self._closed = False
        self._rotation_generation = 0  # increments per completed rotation

    # ---- Introspection ----
    @property
    def current_ws(self) -> Any:
        return self._ws

    @property
    def latest_handle(self) -> Optional[str]:
        return self._latest_handle

    @property
    def goaway_observed(self) -> bool:
        return self._goaway_observed

    @property
    def rotation_generation(self) -> int:
        return self._rotation_generation

    # ---- Open ----
    async def open(self) -> None:
        async with self._lock:
            if self._ws is not None:
                raise RuntimeError("session already open")
            self._ws = await self._connect()
            await self._ws.send(self._build_setup(None))
            staged = await self._await_setup_complete(self._ws)
            # Initial setup: the new WS IS the active session; apply any
            # resumption updates seen during setup through the normal
            # path so `resumable=False` behavior stays consistent.
            for upd in staged:
                self.on_resumption_update(upd)

    async def _await_setup_complete(
        self, ws: Any
    ) -> list[GeminiResumptionUpdate]:
        """Blocks until the WS emits setupComplete.

        Returns the list of resumptionUpdate events seen during setup, in
        order. The CALLER decides when to apply them — do NOT mutate
        active-session state (`_latest_handle`, `_resumable_state`) here,
        because for a replacement WS these updates must only commit AFTER
        a successful swap. For the initial `open()`, the caller applies
        them unconditionally.
        """
        staged: list[GeminiResumptionUpdate] = []
        deadline = asyncio.get_event_loop().time() + self._setup_timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                raise GeminiReplacementError("setup timeout")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            except asyncio.TimeoutError as e:
                raise GeminiReplacementError("setup timeout") from e
            try:
                event = json.loads(raw)
            except Exception:
                continue
            control = parse_control_event(event)
            if control.kind == "setup_complete":
                return staged
            if control.kind == "error":
                raise GeminiReplacementError(
                    f"setup error: {(control.payload or {}).get('message') or 'unknown'}"
                )
            if control.kind == "resumption":
                # Stage into local buffer; do NOT touch session state.
                staged.append(control.payload)
                # ignore until setupComplete arrives

    @property
    def closed(self) -> bool:
        return self._closed

    # ---- Send / recv ----
    async def send(self, frame: str) -> bool:
        """Send one JSON frame on the CURRENT WS.

        Classification is bound to the (ws, rotation_generation) captured
        at entry. On exception we ask: was this failure on the socket we
        actually sent to, or on a socket that was retired while we were
        blocked inside `ws.send()`?

          * `ws is self._ws` AND `gen == self._rotation_generation`
            AND no rotation/goaway marker → genuine failure on the
            CURRENT socket; raises.
          * otherwise (swap committed under us, close() ran, mid-rotation
            or GoAway-pending) → benign drop; returns False.

        Returns:
          True   — frame accepted by the current WS at the time of send.
          False  — frame dropped WITHOUT raising, because the socket was
                   retired, swapped, closed, or is in a transient
                   rotation/goaway window. The frame is NOT retried, and
                   no duplicate reaches the server. Callers must treat
                   False as "transient drop, keep running".

        No retry-on-swap: a replay after an ambiguous send/close could
        duplicate a frame the server already applied, violating the
        "no blind audio replay" rule.
        """
        if self._closed:
            return False
        ws = self._ws
        if ws is None:
            return False
        gen_at_entry = self._rotation_generation
        try:
            await ws.send(frame)
            return True
        except Exception:
            # Was this failure on a socket that got retired under us?
            # Successful rotation increments _rotation_generation AND
            # swaps _ws before closing the old socket, so a delayed
            # close-induced exception from the old ws arrives after both
            # changes. Classify by what we captured, not by live flags.
            if (
                self._closed
                or ws is not self._ws
                or gen_at_entry != self._rotation_generation
            ):
                return False
            # Still-rotating / still-goaway on the SAME current ws:
            # also benign (rotation imminent; drop at boundary).
            if self._rotation_in_flight or self._goaway_observed:
                return False
            raise

    async def recv(self) -> str:
        if self._closed:
            raise RuntimeError("session closed")
        ws = self._ws
        if ws is None:
            raise RuntimeError("session not open")
        return await ws.recv()

    def on_resumption_update(self, upd: GeminiResumptionUpdate) -> None:
        """Caller invokes this after parsing a sessionResumptionUpdate.

        Semantics (per https://ai.google.dev/api/live#SessionResumptionUpdate):
          resumable=True  + new_handle → store the new handle AND mark the
                                          server session recoverable.
          resumable=True  + no handle  → keep stored handle; mark recoverable
                                          (edge case; current checkpoint ok).
          resumable=False              → do NOT overwrite the stored handle,
                                          but mark server state NON-recoverable.
                                          A fresh resumable=True must arrive
                                          BEFORE the GoAway deadline to re-arm
                                          recovery; otherwise rotation refuses.
        """
        if upd.resumable:
            if upd.new_handle:
                self._latest_handle = upd.new_handle
            self._resumable_state = True
        else:
            # Explicit non-resumable server state. Keep any older handle for
            # audit, but mark the state so rotate() will refuse until a fresh
            # resumable=True arrives.
            self._resumable_state = False

    def on_goaway_observed(self) -> None:
        self._goaway_observed = True

    # ---- Rotation ----
    async def rotate(self) -> bool:
        """Replace the current WS with a new one bound to the latest resumable
        handle. Returns True on successful swap; False if there is no
        resumable handle (caller must surface a meaningful error).

        Guarantees:
          * Overlapping rotations are prevented by `_lock`.
          * Replacement WS is fully set up (setupComplete received) BEFORE
            the swap happens. Old WS is closed after swap, bounded by
            `_close_grace`.
          * On any failure before swap, the current WS and its handle are
            unchanged; a GeminiReplacementError is raised to the caller.
        """
        if self._closed:
            return False
        async with self._lock:
            # Dedup within a GoAway cycle. Caller flags _goaway_observed before
            # rotate(); successful swap clears it. A second concurrent rotate()
            # that acquires the lock after swap sees cleared flag → no-op True.
            if not self._goaway_observed:
                return True
            # Safe-recovery policy: the server MUST currently report a
            # resumable state. If the last update was resumable=False (or no
            # update has arrived yet), DO NOT silently reuse an older handle:
            # per the API reference, resuming across a non-resumable gap can
            # LOSE DATA. Refuse rotation; caller surfaces a meaningful error.
            #
            # NOTE on timing: this is an IMMEDIATE refusal. We do NOT
            # continue reading the old connection for a bounded interval
            # awaiting a fresh resumable update after GoAway. The window
            # between GoAway and the server actually closing is advisory
            # and the dispatcher is already blocked here; a bounded wait
            # would require an asynchronous side-channel. If future
            # evidence shows the server reliably emits a fresh resumable
            # update AFTER GoAway, this policy would need revisiting.
            if self._resumable_state is not True:
                return False
            handle = self._latest_handle
            if not handle:
                return False
            # Mark rotation in flight — send() will classify failures as
            # benign drops from this point until the finally clears it.
            self._rotation_in_flight = True
            replacement_task: Optional[asyncio.Task[Any]] = None
            swap_committed = False
            try:
                async def _run_replacement() -> tuple[Any, list[GeminiResumptionUpdate]]:
                    nws = await self._connect()
                    # Record immediately so close() (or our own cleanup)
                    # can tear it down if cancellation interrupts us.
                    self._pending_replacement_ws = nws
                    try:
                        await nws.send(self._build_setup(handle))
                        staged = await self._await_setup_complete(nws)
                    except BaseException:
                        try:
                            await asyncio.wait_for(
                                nws.close(), timeout=self._close_grace
                            )
                        except Exception:
                            pass
                        raise
                    return nws, staged

                replacement_task = asyncio.create_task(_run_replacement())
                self._replacement_task = replacement_task
                try:
                    new_ws, staged = await asyncio.wait_for(
                        asyncio.shield(replacement_task),
                        timeout=self._replacement_total_timeout,
                    )
                except asyncio.TimeoutError as e:
                    # Owned cleanup: cancel AND await the shielded task
                    # before raising. Then close any WS it acquired before
                    # the cancel reached it. Timeout-caller expects the
                    # error, so we drive the cleanup here.
                    await self._cleanup_pending_replacement(replacement_task)
                    raise GeminiReplacementError("replacement timeout") from e
                except asyncio.CancelledError:
                    # OUTER cancellation (dispatcher cancelled). Do NOT
                    # cancel the shielded task from inside the cancelled
                    # context — subsequent awaits would be re-interrupted
                    # and would leak the task. Preserve the tracking
                    # references so session.close() can finish cleanup.
                    # Re-raise so the caller's cancellation propagates.
                    raise

                # If close() ran during replacement setup, do NOT swap —
                # drive the shared cleanup so refs clear and new_ws closes.
                if self._closed:
                    await self._cleanup_pending_replacement(replacement_task)
                    return False

                # Swap atomically (we're under lock).
                old_ws = self._ws
                self._ws = new_ws
                self._pending_replacement_ws = None
                self._goaway_observed = False
                self._rotation_generation += 1
                swap_committed = True

                # Only AFTER the swap: apply any staged resumption updates
                # from the replacement's setup. Failure never reaches here.
                for upd in staged:
                    self.on_resumption_update(upd)
            finally:
                self._rotation_in_flight = False
                # Ownership-based clearing — NOT `task.done()`-based.
                # Task-done does not imply the WS it returned was
                # transferred to self._ws or closed. If the swap did not
                # commit (cancellation after child completion, or we
                # never created a task), leave the tracking references
                # intact so close() can find and dispose any WS the
                # task returned via `_cleanup_pending_replacement`.
                if swap_committed or replacement_task is None:
                    self._pending_replacement_ws = None
                    if self._replacement_task is replacement_task:
                        self._replacement_task = None

        # Drain / close old WS outside the lock to avoid blocking subsequent
        # operations on the new WS.
        if old_ws is not None:
            try:
                await asyncio.wait_for(old_ws.close(), timeout=self._close_grace)
            except Exception:
                pass
        return True

    async def _cleanup_pending_replacement(
        self, task: Optional[asyncio.Task[Any]]
    ) -> None:
        """Cancel a running replacement task, await it to completion, and
        close any WS it managed to acquire (either recorded on
        `_pending_replacement_ws` before cancellation reached it, or
        returned as the task's result if the swap decision was skipped).
        Safe to call multiple times; always clears tracking refs.
        """
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except BaseException:
                pass
        # Any WS the task opened before cancel reached it.
        pending = self._pending_replacement_ws
        if pending is not None:
            try:
                await asyncio.wait_for(pending.close(), timeout=self._close_grace)
            except Exception:
                pass
        # If the task completed successfully but we didn't swap, close
        # the WS it returned.
        if task is not None and task.done() and not task.cancelled():
            try:
                result = task.result()
                if isinstance(result, tuple) and len(result) >= 1:
                    maybe_ws = result[0]
                    if maybe_ws is not None and maybe_ws is not pending:
                        try:
                            await asyncio.wait_for(
                                maybe_ws.close(), timeout=self._close_grace
                            )
                        except Exception:
                            pass
            except BaseException:
                pass
        # Clear tracking refs.
        self._pending_replacement_ws = None
        if self._replacement_task is task:
            self._replacement_task = None

    async def close(self) -> None:
        """Shutdown contract: idempotent, closes the current WS, closes
        any in-flight replacement WS, and cancels+awaits the replacement
        task if one is running. No external gate release is required.
        Works whether the replacement task was abandoned by an outer
        cancellation of rotate() or is still progressing normally."""
        self._closed = True

        # Replacement task + any WS it acquired. Shared cleanup helper
        # handles both the "task still awaiting connector" case and the
        # "task finished right before close() ran" case.
        task = self._replacement_task
        await self._cleanup_pending_replacement(task)

        # Current WS.
        ws = self._ws
        self._ws = None
        if ws is not None:
            try:
                await asyncio.wait_for(ws.close(), timeout=self._close_grace)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Shared dispatch loop
#
# The receive loop runs the SAME code path in production (main.py handler)
# and tests. This file is the ONLY definition of control-event dispatch
# + rotation handoff — there is no duplicate replica in tests. Any wiring
# bug therefore shows up in both places and is caught by handler-level
# tests.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DispatchCallbacks:
    """Callbacks the dispatch loop invokes. All must be async.

    `send_error(message)`: emit a user-visible `type:"error"` message.
    `on_content(content)`: handle a GeminiServerContent (transcripts, audio,
                           turn_complete, interrupted).
    `is_closed()`:         sync bool predicate — caller's shutdown flag.
    """
    send_error: Callable[[str], Awaitable[None]]
    on_content: Callable[[GeminiServerContent], Awaitable[None]]
    is_closed: Callable[[], bool]


class HandlerBuffers:
    """Mutable transcript buffer state owned by the handler.

    Kept as a class (not dataclass) because the on_content callback
    mutates `source`/`output` across invocations.
    """

    __slots__ = ("source", "output")

    def __init__(self) -> None:
        self.source: str = ""
        self.output: str = ""


def build_handler_callbacks(
    buffers: HandlerBuffers,
    *,
    send_to_producer: Callable[[dict[str, Any]], Awaitable[None]],
    broadcast_translation: Callable[[str, bool, str], Awaitable[None]],
    broadcast_audio: Callable[[str], Awaitable[None]],
    commit_output: Callable[[str], Awaitable[None]],
    on_interrupted: Callable[[], None],
    is_closed: Callable[[], bool],
    schedule_idle_flush: Optional[Callable[[], None]] = None,
) -> DispatchCallbacks:
    """Build the DispatchCallbacks the production handler uses.

    Centralized here (not inside the handler) so tests can construct an
    identical callback bundle with mock primitives and verify the exact
    production wiring: how transcripts merge, when audio is broadcast,
    when turn_complete commits, how interrupted clears buffers.

    The production handler in main.py calls this; the integration test
    in test_gemini_live_handler calls this. One source of truth.
    """

    async def _send_error(message: str) -> None:
        await send_to_producer({"type": "error", "message": message})

    async def _on_content(content: GeminiServerContent) -> None:
        if content.interrupted:
            buffers.source = ""
            buffers.output = ""
            on_interrupted()

        if content.input_transcript:
            buffers.source = merge_transcript(buffers.source, content.input_transcript)
            await send_to_producer(
                {"type": "stt.partial", "text": buffers.source.strip()}
            )

        if content.output_transcript:
            buffers.output = merge_transcript(buffers.output, content.output_transcript)
            await broadcast_translation(buffers.output, True, buffers.source)
            if schedule_idle_flush is not None:
                schedule_idle_flush()

        for audio_data in content.audio_chunks:
            await broadcast_audio(audio_data)

        if content.turn_complete:
            await commit_output("turn_complete")

    return DispatchCallbacks(_send_error, _on_content, is_closed)


async def run_recovery_dispatch(
    session: GeminiLiveSession,
    callbacks: DispatchCallbacks,
) -> None:
    """Receive-loop + control-event dispatch + rotation handoff.

    Terminates on:
      * server-sent error event    → forwards to callbacks.send_error, breaks
      * replacement failure         → forwards "recovery failed" error, breaks
      * no-resumable-handle on goAway → forwards specific error, breaks
      * normal close (code=1000)    → breaks silently
      * caller shutdown             → is_closed() → True → breaks silently
      * any other close/exception   → forwards "disconnected" error, breaks

    Terminates WITHOUT breaking:
      * rotation_generation moved past snapshot (expected drain of old WS)
    """
    expected_rotation_gen = session.rotation_generation
    while True:
        try:
            raw = await session.recv()
        except Exception as recv_exc:
            code = getattr(recv_exc, "code", None)
            if code == 1000:
                return
            current_gen = session.rotation_generation
            if current_gen > expected_rotation_gen and not callbacks.is_closed():
                expected_rotation_gen = current_gen
                continue
            if callbacks.is_closed():
                return
            await callbacks.send_error(
                f"Gemini Live Translate disconnected: {str(recv_exc)[:180]}"
            )
            return

        try:
            event = json.loads(raw)
        except Exception:
            continue

        control = parse_control_event(event)

        if control.kind == "error":
            message = (control.payload or {}).get("message") or "Gemini Live Translate error"
            await callbacks.send_error(message)
            return

        if control.kind == "resumption":
            session.on_resumption_update(control.payload)
            continue

        if control.kind == "goaway":
            session.on_goaway_observed()
            try:
                rotated = await session.rotate()
            except GeminiReplacementError as rerr:
                await callbacks.send_error(
                    f"Gemini Live Translate recovery failed: {str(rerr)[:180]}"
                )
                return
            if not rotated:
                # Distinguish the two reasons: no resumable state (server-marked
                # non-recoverable or never resumable) vs explicit close race.
                if callbacks.is_closed():
                    return
                await callbacks.send_error(
                    "Gemini Live Translate session ended: current state is not resumable"
                )
                return
            expected_rotation_gen = session.rotation_generation
            continue

        if control.kind == "setup_complete":
            # Mid-stream setupComplete is normal after a successful rotation.
            continue

        if control.kind != "content":
            continue

        await callbacks.on_content(control.payload)


# backend/app/main.py
import os, json, asyncio, logging, time, re
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# --- local modules (single import each) ---
from app.socket_manager import manager
from app.deepgram_session import connect_to_deepgram
from app.utils.translate import translate_text, TranslationContext  # async wrapper you already have
from app.scripture import detect_scripture_verse
from app.routes import translate as translate_routes  # your existing REST routes
from app.routes import examples as examples_routes

# ------------------------------------------------------------------------------
# ONE app only
# ------------------------------------------------------------------------------
app = FastAPI(title="Real-Time Translation Backend", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # relax for dev; tighten for prod
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Keep your existing HTTP routes under /api
app.include_router(translate_routes.router, prefix="/api")
app.include_router(examples_routes.router, prefix="/api")

@app.get("/")
def root():
    return {"ok": True, "msg": "server is live"}

# ------------------------------------------------------------------------------
# Consumer hub: /ws/translate
#  - Frontend connects here (useTranslationSocket)
#  - Stays connected; usually sends only {"type":"consumer_join"}
# ------------------------------------------------------------------------------
@app.websocket("/ws/translate")
async def ws_translate(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            # Keep the socket alive; most consumers won't send anything after join.
            try:
                raw = await ws.receive_text()
            except WebSocketDisconnect:
                break
            except Exception:
                # Ignore weird frames so the connection persists
                continue
            # Optionally ignore a consumer_join message; nothing else to do here.
            # We don't require source/target here.
    finally:
        manager.disconnect(ws)

# ------------------------------------------------------------------------------
# Producer: /ws/stt/deepgram
#  - Browser streams PCM → backend → Deepgram
#  - We show partials to producer (for the textarea)
#  - On Deepgram is_final=True, we translate and broadcast to all consumers
# ------------------------------------------------------------------------------
# ---- replace your entire ws_stt_deepgram with this ----

def _clean_lang(raw: Optional[str], default: str) -> str:
    if raw is None:
        return default
    cleaned = raw.strip().lower()
    return cleaned or default


def _normalize_lang(raw: Optional[str], default: str) -> str:
    if not raw:
        return default
    cleaned = raw.strip().lower()
    if not cleaned:
        return default
    primary = cleaned.split("-")[0]
    return primary or default


@app.websocket("/ws/stt/deepgram")
async def ws_stt_deepgram(websocket: WebSocket):
    src_lang_full = _clean_lang(websocket.query_params.get("source"), "ko")
    tgt_lang_full = _clean_lang(websocket.query_params.get("target"), "en")
    src_lang = _normalize_lang(src_lang_full, "ko")
    tgt_lang = _normalize_lang(tgt_lang_full, "en")
    dg_language = _deepgram_language_preference(src_lang_full)
    dg_keywords = None if src_lang.startswith("ko") else []
    await websocket.accept()
    translation_ctx = TranslationContext()
    try:
        dg = await connect_to_deepgram(language=dg_language, keywords=dg_keywords)  # <-- dg is created here
    except Exception as e:
        await websocket.send_json({"type": "error", "message": f"Deepgram connect failed: {e}"})
        await websocket.close()
        return

    seq = 0
    closed = asyncio.Event()
    finalize_event = asyncio.Event()

    async def from_client_to_deepgram():
        try:
            while True:
                msg = await websocket.receive()
                if msg.get("type") == "websocket.disconnect":
                    try:
                        await dg.close()
                    except:
                        pass
                    break
                if (b := msg.get("bytes")):
                    # your AudioWorklet streams raw 16-bit PCM @ 48k
                    await dg.send(b)
                elif (t := msg.get("text")):
                    # allow client-side finalize
                    try:
                        payload = json.loads(t)
                        if payload.get("type") == "finalize":
                            finalize_event.set()
                            continue
                    except:
                        pass
        finally:
            closed.set()

    async def from_deepgram_to_server():
        """
        Option A: translate only when a sentence is complete.
        Commit rules:
          - speech_final=True  → commit immediately
          - or final text ends with sentence punctuation → commit
          - else start/refresh a ~1.2s timer; on timeout, commit whatever we have
        """
        SENTENCE_PUNCT = tuple(".?!。？！…")
        SENTENCE_PUNCT_CHARS = "".join(SENTENCE_PUNCT)
        COMMIT_WAIT_MS = 250
        CJK_PENDING_HOLD_MS = 600
        MIN_CONFIDENT_CHARS = 10
        KOREAN_SHORT_MIN_CHARS = 14
        KOREAN_EOS_RE = re.compile(
            r"(?:습니다|입니다|합니다|했습니다|할까요|했어요|했지요|했네요|예요|이에요|에요|일까요|였어요|였습니까|입니까|됩니까|나요|군요|지요|래요|랍니다|라네요|다|아요|어요|에요)$"
        )

        pending_src: str | None = None
        pending_speech_final = False
        pending_task: asyncio.Task | None = None

        def ends_like_sentence(t: str) -> bool:
            t = (t or "").rstrip()
            if not t:
                return False
            if src_lang.startswith("ko"):
                stripped = t.rstrip(SENTENCE_PUNCT_CHARS)
                if not stripped:
                    return False
                return bool(KOREAN_EOS_RE.search(stripped))
            last_char = t[-1]
            if last_char in SENTENCE_PUNCT:
                return True
            return False

        def norm_ws(s: str) -> str:
            return " ".join((s or "").split())

        def is_short_korean_clause(text: str) -> bool:
            if not text or not src_lang.startswith("ko"):
                return False
            clean = norm_ws(text)
            return len(clean) < KOREAN_SHORT_MIN_CHARS

        def looks_complete(text: str) -> bool:
            clean = norm_ws(text)
            if not clean:
                return False
            if ends_like_sentence(clean):
                return True
            if src_lang.startswith(CJK_NO_SPACE_PREFIXES):
                return False
            return len(clean) >= MIN_CONFIDENT_CHARS

        def should_apply_cjk_hold(text: str) -> bool:
            if not text:
                return False
            if not src_lang.startswith(CJK_NO_SPACE_PREFIXES):
                return False
            return not ends_like_sentence(text)

        def should_hold_short_korean(text: str, speech_final_flag: bool) -> bool:
            if speech_final_flag:
                return False
            return is_short_korean_clause(text)

        SUBSET_SUPPRESS_WINDOW_SEC = 4.0
        MIN_SUBSET_DELTA = 6
        CJK_NO_SPACE_PREFIXES = ("ko", "zh", "ja")

        async def commit_now(src_text_raw: str):
            nonlocal seq, pending_src, pending_task, pending_speech_final
            if not src_text_raw or not src_text_raw.strip():
                return

            normalized = norm_ws(src_text_raw)
            if not normalized:
                return

            last_norm = getattr(commit_now, "_last_norm", "")
            last_ts = getattr(commit_now, "_last_commit_ts", 0.0)
            if normalized == last_norm:
                return

            if last_norm and len(normalized) < len(last_norm):
                delta = len(last_norm) - len(normalized)
                subset_lang = src_lang.startswith(CJK_NO_SPACE_PREFIXES)
                no_space = " " not in normalized and " " not in last_norm
                if subset_lang and no_space and delta >= MIN_SUBSET_DELTA:
                    is_edge_subset = last_norm.startswith(normalized) or last_norm.endswith(normalized)
                    recent_commit = (time.time() - last_ts) < SUBSET_SUPPRESS_WINDOW_SEC
                    if is_edge_subset and recent_commit:
                        print("[A][skip][subset]", normalized)
                        pending_src = None
                        pending_speech_final = False
                        if pending_task and not pending_task.done():
                            pending_task.cancel()
                        pending_task = None
                        return

            setattr(commit_now, "_last_norm", normalized)
            setattr(commit_now, "_last_commit_ts", time.time())
            setattr(commit_now, "_last_src", src_text_raw)

            seq += 1
            src_text = src_text_raw
            meta_payload = {
                "mode": "realtime",
                "partial": False,
                "segment_id": seq,
                "rev": 0,
                "seq": seq,
                "is_final": True,
            }

            scripture_hit = None
            if src_lang.startswith("ko"):
                try:
                    scripture_hit = detect_scripture_verse(src_text)
                except Exception as exc:
                    print("[SCRIPTURE][error]", exc)

            if scripture_hit:
                translated = scripture_hit.text
                meta_payload.update(
                    {
                        "kind": "scripture",
                        "reference": scripture_hit.reference,
                        "reference_ko": scripture_hit.source_reference,
                        "reference_en": scripture_hit.reference_en or scripture_hit.reference,
                        "version": scripture_hit.version,
                        "source_version": scripture_hit.source_version,
                        "book": scripture_hit.book,
                        "book_en": scripture_hit.book_en or scripture_hit.book,
                        "chapter": scripture_hit.chapter,
                        "verse": scripture_hit.verse,
                        "end_verse": scripture_hit.end_verse,
                        "source_text": scripture_hit.source_text,
                    }
                )
                print(f"[SCRIPTURE] matched {scripture_hit.reference}")
            elif src_lang == tgt_lang and src_lang_full == tgt_lang_full:
                translated = src_text
            else:
                try:
                    translated = await translate_text(src_text, src_lang_full, tgt_lang_full, ctx=translation_ctx)
                    translation_ctx.last_english = translated
                except Exception as e:
                    print("[TX] error:", e)
                    translated = src_text  # fail-open

            print(f"[A] FINAL seq={seq} {src_lang_full}->{tgt_lang_full} src='{src_text}' → tgt='{translated}'")

            # shape your client already supports
            live_msg_new = {
                "mode": "live",
                "text": translated,
                "seq": seq,
                "src": {"text": src_text, "lang": src_lang_full},
                "tgt": {"lang": tgt_lang_full},
                "meta": meta_payload.copy(),
            }
            live_msg_legacy = {
                "type": "translation",
                "payload": translated,
                "lang": tgt_lang_full,
                "meta": meta_payload.copy(),
            }

            try:
                await websocket.send_json(live_msg_new)
                await websocket.send_json(live_msg_legacy)
            except Exception as e:
                print("[DG] send back to producer failed:", e)

            try:
                await manager.broadcast(live_msg_new)
                await manager.broadcast(live_msg_legacy)
                print(f"[BROADCAST] seq={seq} '{translated[:60]}'")
            except Exception as e:
                print("[DG] broadcast error:", e)

            pending_src = None
            pending_speech_final = False
            if pending_task and not pending_task.done():
                pending_task.cancel()
            pending_task = None

        async def arm_timer(wait_override_ms: int | None = None):
            nonlocal pending_task
            if pending_task and not pending_task.done():
                pending_task.cancel()

            snap = pending_src or ""
            wait_ms = wait_override_ms if wait_override_ms is not None else COMMIT_WAIT_MS

            async def _wait_and_commit(snap: str, delay_ms: int):
                try:
                    await asyncio.sleep(delay_ms / 1000.0)
                    if pending_src and norm_ws(pending_src) == norm_ws(snap):
                        await commit_now(pending_src)
                except asyncio.CancelledError:
                    pass

            pending_task = asyncio.create_task(_wait_and_commit(snap, wait_ms))

        async def flush_on_finalize():
            while True:
                try:
                    await finalize_event.wait()
                except asyncio.CancelledError:
                    break
                finalize_event.clear()
                if pending_src:
                    try:
                        if should_hold_short_korean(pending_src, pending_speech_final):
                            await arm_timer(CJK_PENDING_HOLD_MS)
                        else:
                            await commit_now(pending_src)
                    except Exception:
                        pass

        finalize_task = asyncio.create_task(flush_on_finalize())

        try:
            async for raw in dg:  # <-- dg is in scope (captured from outer function)
                try:
                    evt = json.loads(raw)
                except Exception:
                    continue
                if evt.get("type") != "Results":
                    continue

                ch = evt.get("channel") or {}
                alts = ch.get("alternatives") or []
                if not alts:
                    continue

                best = alts[0]
                transcript = (best.get("transcript") or "").strip()
                words = best.get("words") or []

                # Prefer word-level reconstruction to recover Korean spacing in partials/finals
                if src_lang.startswith("ko") and words:
                    joined_words = " ".join(
                        (w.get("punctuated_word") or w.get("word") or "").strip()
                        for w in words
                        if (w.get("word") or "").strip()
                    ).strip()
                    if joined_words:
                        if (" " not in transcript) or (len(joined_words) >= len(transcript) - 2):
                            transcript = joined_words

                is_final = bool(evt.get("is_final"))
                speech_final = bool(evt.get("speech_final") or False)

                # show partial text in the UI, but DO NOT translate yet
                if transcript and not is_final:
                    try:
                        await websocket.send_json({"type": "stt.partial", "text": transcript})
                    except:
                        pass
                    continue

                if not is_final:
                    continue

                if transcript:
                    pending_src = transcript
                    pending_speech_final = speech_final

                print(f"[DG][A] final: speech_final={speech_final} src='{pending_src or ''}'")

                if speech_final and pending_src:
                    if looks_complete(pending_src):
                        await commit_now(pending_src)
                    else:
                        hold_ms = CJK_PENDING_HOLD_MS if should_apply_cjk_hold(pending_src) else None
                        await arm_timer(hold_ms)
                    continue

                if pending_src and ends_like_sentence(pending_src):
                    if should_hold_short_korean(pending_src, speech_final):
                        await arm_timer(CJK_PENDING_HOLD_MS)
                    else:
                        await commit_now(pending_src)
                    continue

                if pending_src:
                    hold_ms = CJK_PENDING_HOLD_MS if should_apply_cjk_hold(pending_src) else None
                    await arm_timer(hold_ms)

        finally:
            finalize_task.cancel()
            try:
                await finalize_task
            except asyncio.CancelledError:
                pass
            # best-effort flush on shutdown
            if pending_src:
                try:
                    await commit_now(pending_src)
                except Exception:
                    pass

    consumer = asyncio.create_task(from_client_to_deepgram())
    producer = asyncio.create_task(from_deepgram_to_server())
    await closed.wait()
    try:
        consumer.cancel()
        producer.cancel()
    except:
        pass

# ------------------------------------------------------------------------------
# Quick debug to prove the FE consumer is listening
# ------------------------------------------------------------------------------
@app.get("/debug/broadcast")
async def debug_broadcast():
    msg_new = {"mode": "live", "text": "**TEST BROADCAST**", "seq": 999, "tgt": {"lang": "en"}}
    msg_legacy = {
        "type": "translation",
        "payload": "**TEST BROADCAST**",
        "lang": "en",
        "meta": {"mode": "realtime", "partial": False, "segment_id": 999, "rev": 0, "seq": 999},
    }
    await manager.broadcast(msg_new)
    await manager.broadcast(msg_legacy)
    return {"ok": True}
DEFAULT_DG_LANGUAGE = os.getenv("DEEPGRAM_LANGUAGE", "ko")


def _deepgram_language_preference(raw: Optional[str]) -> str:
    """
    Map UI language codes to Deepgram's expected identifiers.
    Falls back to the configured default if nothing matches.
    """
    if not raw:
        return DEFAULT_DG_LANGUAGE
    token = raw.strip().lower()
    if not token:
        return DEFAULT_DG_LANGUAGE

    overrides = {
        "en": "en",
        "en-us": "en",
        "en-gb": "en",
        "ko": "ko",
        "ko-kr": "ko",
        "es": "es",
        "es-es": "es",
        "zh": "zh",
        "zh-cn": "zh",
        "zh-hans": "zh",
        "zh-tw": "zh",
        "zh-hant": "zh",
    }
    if token in overrides:
        return overrides[token]

    primary = token.split("-")[0]
    if primary in overrides:
        return overrides[primary]

    return primary or DEFAULT_DG_LANGUAGE

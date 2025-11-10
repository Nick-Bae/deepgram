# # app/main.py
# import inspect, sys
# import app.deepgram_session as _dgs
# print("[DG][import] deepgram_session from:", _dgs.__file__)
# print("[DG][import] websockets present?:", "websockets" in sys.modules)
# try:
#     import websockets
#     print("[DG][import] websockets path:", websockets.__file__)
#     print("[DG][import] websockets.connect:", inspect.getsource(websockets.connect).splitlines()[0])
# except Exception as e:
#     print("[DG][import] websockets import failed:", e)


# import os, json
# import asyncio
# import re
# from typing import List, Optional, Literal, Dict

# from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Request, HTTPException, Body, APIRouter  # ✅ APIRouter added
# from fastapi.middleware.cors import CORSMiddleware
# from dotenv import load_dotenv
# from pydantic import BaseModel, Field
# from app.socket_manager import manager
# from app.deepgram_session import connect_to_deepgram
# from app.commit import Committer
# from app.aggregate import SentenceAggregator
# from app.utils.translate import translate_text

# # Your existing modules
# from app.routes import translate as translate_routes
# from app.socket_manager import manager
# from app.utils.translate import translate_text as _translate_sync  # your current translator

# import inspect, logging
# log = logging.getLogger("rt")  # ✅ logger defined

# # ---- translator alias (works with translate_text or translate_api; sync/async) ----
# try:
#     from app.utils.translate import translate_text as _translate_impl  # type: ignore
# except Exception:
#     from app.utils.translate import translate_api as _translate_impl  # type: ignore

# async def translate_text(text: str, src: str, tgt: str) -> str:
#     res = _translate_impl(text, src, tgt)
#     if inspect.isawaitable(res):
#         return await res
#     return res

# def norm(code: str | None) -> str:
#     return (code or '').lower().split('-')[0]

# # ---------------------------
# # App setup
# # ---------------------------
# app = FastAPI(title="Hybrid Real-Time Translation Backend", version="0.3.0")
# router = APIRouter()  # ✅ router created
# load_dotenv()

# # Allow dev origins (you can tighten later)
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],          # ✅ open for dev; restrict in prod
#     allow_credentials=True,
#     allow_methods=["*"],
#     allow_headers=["*"],
# )

# # Keep your existing HTTP routes
# app.include_router(translate_routes.router, prefix="/api")

# @app.get("/")
# def read_root():
#     return {"message": "Server is live"}

# # ---------------------------
# # ===== Hybrid Mode Additions =====
# # ---------------------------
# try:
#     from sklearn.feature_extraction.text import TfidfVectorizer
#     from sklearn.metrics.pairwise import cosine_similarity
#     _HAS_SK = True
# except Exception:
#     _HAS_SK = False

# class ScriptPair(BaseModel):
#     source: str = Field(..., description="Original sentence in Korean")
#     target: str = Field(..., description="Pre-translated sentence in English")

# class UploadScriptRequest(BaseModel):
#     pairs: List[ScriptPair]

# class MatchConfig(BaseModel):
#     threshold: float = Field(0.84, ge=0.0, le=1.0)
#     method: Literal["tfidf"] = "tfidf"

# class TranslateIn(BaseModel):
#     text: str
#     source_lang: str = "ko"
#     target_lang: str = "en"

# class TranslateOut(BaseModel):
#     translated: str
#     mode: Literal["pre", "realtime"]
#     match_score: float
#     matched_source: Optional[str] = None
#     method: str = "tfidf"
#     original: Optional[str] = None

# _punct_re = re.compile(r"[\u3000\s]+")
# _quotes_re = re.compile(r"[\u2018\u2019\u201C\u201D]")

# def normalize(s: str) -> str:
#     s = s.strip()
#     s = _quotes_re.sub('"', s)
#     s = _punct_re.sub(' ', s)
#     return s

# class ScriptStore:
#     def __init__(self):
#         self.pairs: List[ScriptPair] = []
#         self._vectorizer = None
#         self._matrix = None
#         self._norm_sources: List[str] = []
#         self.config = MatchConfig()

#     def load(self, pairs: List[ScriptPair]):
#         if not _HAS_SK:
#             raise HTTPException(status_code=500, detail="scikit-learn not installed; cannot build matcher")
#         self.pairs = pairs
#         self._norm_sources = [normalize(p.source) for p in pairs]
#         self._vectorizer = TfidfVectorizer(ngram_range=(1, 2), analyzer="char_wb", min_df=1)
#         self._matrix = self._vectorizer.fit_transform(self._norm_sources)

#     def clear(self):
#         self.pairs = []
#         self._vectorizer = None
#         self._matrix = None
#         self._norm_sources = []

#     def is_ready(self) -> bool:
#         return bool(self.pairs) and self._vectorizer is not None and self._matrix is not None

#     def best_match(self, text: str):
#         if not self.is_ready():
#             return 0.0, None, None
#         q = normalize(text)
#         qv = self._vectorizer.transform([q])
#         sims = cosine_similarity(qv, self._matrix)[0]
#         idx = int(sims.argmax())
#         score = float(sims[idx])
#         matched_src = self._norm_sources[idx]
#         return score, self.pairs[idx], matched_src

# STORE = ScriptStore()

# # Use your existing translator in a thread-safe async wrapper (not used by WS alias, but kept)
# async def translate_fallback(text: str, source_lang: str = "ko", target_lang: str = "en") -> str:
#     return await asyncio.to_thread(_translate_sync, text, source_lang, target_lang)

# @app.get("/api/health")
# def health():
#     return {"status": "ok", "script_loaded": STORE.is_ready(), "pairs": len(STORE.pairs)}

# @app.post("/api/script/upload")
# def upload_script(payload: UploadScriptRequest, cfg: MatchConfig = Body(default=MatchConfig())):
#     if not payload.pairs:
#         raise HTTPException(status_code=400, detail="No pairs provided")
#     STORE.load(payload.pairs)
#     STORE.config = cfg
#     return {"ok": True, "loaded": len(payload.pairs), "method": cfg.method, "threshold": cfg.threshold}

# @app.delete("/api/script")
# def clear_script():
#     STORE.clear()
#     return {"ok": True}

# @app.post("/api/translate", response_model=TranslateOut)
# async def translate_api(inp: TranslateIn):
#     if STORE.is_ready():
#         score, pair, matched_src = STORE.best_match(inp.text)
#         if score >= STORE.config.threshold and pair:
#             return TranslateOut(
#                 translated=pair.target,
#                 mode="pre",
#                 match_score=score,
#                 matched_source=matched_src,
#                 method=STORE.config.method,
#                 original=inp.text,
#             )
#     translated = await translate_fallback(inp.text, inp.source_lang, inp.target_lang)
#     return TranslateOut(
#         translated=translated,
#         mode="realtime",
#         match_score=0.0,
#         matched_source=None,
#         method=STORE.config.method,
#         original=inp.text,
#     )

# # ---------------------------
# # WebSocket: producer + listeners
# # ---------------------------
# # app/main.py (replace only the ws_translate function)
# @router.websocket("/ws/translate")
# async def ws_translate(ws: WebSocket):
#     await manager.connect(ws)

#     # Per-connection state (by ASR segment id)
#     seg_base_kr: dict[int, str] = {}          # committed KR accumulated for each seg
#     seg_spoken_en: dict[int, str] = {}        # last full EN we've already "spoken" (for delta)
#     seg_last_preview_en: dict[int, str] = {}  # last preview EN sent (to dedupe)

#     partial_task: asyncio.Task | None = None
#     debounce_task: asyncio.Task | None = None

#     def _norm_ws(s: str) -> str:
#         return " ".join((s or "").split())

#     def _strip_kr_prefix(full: str, prefix: str) -> str:
#         """Remove KR prefix ignoring whitespace; return remaining tail in original spacing."""
#         f, p = _norm_ws(full), _norm_ws(prefix)
#         if not p or not f.startswith(p):
#             return full
#         want = len(p)  # count non-space chars of prefix within original full
#         seen = 0
#         for i, ch in enumerate(full):
#             if not ch.isspace():
#                 seen += 1
#             if seen >= want:
#                 return full[i + 1 :].lstrip()
#         return ""

#     def _en_delta(full_en: str, prev_en: str) -> str:
#         """Return the English tail not yet spoken; fall back to full_en if prefix doesn't match."""
#         f = _norm_ws(full_en)
#         p = _norm_ws(prev_en)
#         if not p:
#             return full_en
#         if not f.startswith(p):
#             # If the model rephrased earlier part, just send the new full string once.
#             return full_en
#         # map prefix length back onto original string to keep spacing/punctuation
#         want = len(p)
#         seen = 0
#         for i, ch in enumerate(full_en):
#             if not ch.isspace():
#                 seen += 1
#             if seen >= want:
#                 return full_en[i + 1 :].lstrip()
#         return ""

#     async def _broadcast(text_en: str, tgt: str, *, is_partial: bool, seg_id, rev, mode="realtime"):
#         await manager.broadcast({
#             "type": "translation",
#             "payload": text_en,
#             "lang": tgt,
#             "meta": {
#                 "partial": is_partial,
#                 "segment_id": seg_id,
#                 "rev": rev,
#                 "mode": mode,
#                 "translated": text_en,
#             },
#         })

#     async def _translate(text: str, src: str, tgt: str) -> str:
#         return await translate_text(text, src, tgt)

#     async def schedule_partial(text: str, src: str, tgt: str, seg_id, rev):
#         """Translate base+partial with a tiny debounce; dedupe identical previews."""
#         nonlocal partial_task, debounce_task

#         if debounce_task and not debounce_task.done():
#             debounce_task.cancel()
#         if partial_task and not partial_task.done():
#             partial_task.cancel()

#         base = seg_base_kr.get(int(seg_id or 0), "")
#         combined_kr = (base + (" " if base and text and not base.endswith(" ") else "") + (text or "")).strip()
#         if not combined_kr:
#             return

#         async def _debounced():
#             try:
#                 await asyncio.sleep(0.12)  # ~120ms to avoid translating every keystroke-like interim
#                 en_full = await _translate(combined_kr, src, tgt)
#                 last = seg_last_preview_en.get(int(seg_id or 0), "")
#                 if _norm_ws(en_full) != _norm_ws(last):
#                     seg_last_preview_en[int(seg_id or 0)] = en_full
#                     await _broadcast(en_full, tgt, is_partial=True, seg_id=seg_id, rev=rev)
#             except asyncio.CancelledError:
#                 pass

#         debounce_task = asyncio.create_task(_debounced())

#     try:
#         while True:
#             msg = await ws.receive_json()
#             mtype = msg.get("type")
#             src = norm(msg.get("source"))
#             tgt = norm(msg.get("target"))
#             seg_id = int(msg.get("id") or 0)
#             rev = int(msg.get("rev") or 0)

#             if mtype == "producer_partial":
#                 # Preview = translate (base KR + current partial KR)
#                 await schedule_partial(msg.get("text", ""), src, tgt, seg_id, rev)
#                 continue

#             if mtype == "producer_commit":
#                 text = (msg.get("text") or "").strip()
#                 if not text:
#                     continue
#                 is_final = bool(msg.get("final"))

#                 # Cancel any in-flight preview jobs
#                 if debounce_task and not debounce_task.done():
#                     debounce_task.cancel()
#                 if partial_task and not partial_task.done():
#                     partial_task.cancel()

#                 if not is_final:
#                     # Non-final commit ⇒ extend base KR for this seg
#                     current_base = seg_base_kr.get(seg_id, "")
#                     new_base = (current_base + (" " if current_base and text and not current_base.endswith(" ") else "") + text).strip()
#                     seg_base_kr[seg_id] = new_base

#                     # Compute contextual English using the full KR so far,
#                     # but only SPEAK the delta relative to what we've already spoken.
#                     en_full = await _translate(new_base, src, tgt)
#                     prev_spoken = seg_spoken_en.get(seg_id, "")
#                     en_tail = _en_delta(en_full, prev_spoken)
#                     if _norm_ws(en_tail):
#                         await _broadcast(en_tail, tgt, is_partial=False, seg_id=seg_id, rev=rev)
#                         seg_spoken_en[seg_id] = en_full  # advance the spoken prefix
#                     continue

#                 # ASR final for this seg
#                 # Sometimes ASR final is the FULL seg text; if we had partial commits,
#                 # send only the delta not yet spoken.
#                 kr_base = seg_base_kr.get(seg_id, "")
#                 # prefer the *full* ASR final text if available, else base
#                 kr_full = text if _norm_ws(text) else kr_base
#                 if not _norm_ws(kr_full):
#                     continue

#                 en_full = await _translate(kr_full, src, tgt)
#                 prev_spoken = seg_spoken_en.get(seg_id, "")
#                 en_tail = _en_delta(en_full, prev_spoken)
#                 if _norm_ws(en_tail):
#                     await _broadcast(en_tail, tgt, is_partial=False, seg_id=seg_id, rev=rev)

#                 # Clear per-seg state
#                 seg_base_kr.pop(seg_id, None)
#                 seg_spoken_en.pop(seg_id, None)
#                 seg_last_preview_en.pop(seg_id, None)
#                 continue

#             elif mtype == "consumer_join":
#                 # no-op; present for completeness
#                 continue

#             else:
#                 # unknown message type; ignore
#                 continue

#     except Exception as e:
#         # don’t crash the server because a socket died
#         logging.getLogger("rt").warning(f"ws closed: {e}")
#     finally:
#         manager.disconnect(ws)



# # ---------------------------
# # Keep your existing broadcast endpoint
# # ---------------------------
# @app.post("/api/broadcast")
# async def broadcast_translation(request: Request):
#     data = await request.json()
#     text = data.get("text", "")
#     lang = data.get("lang", "en")

#     payload = {
#         "type": "translation",
#         "payload": text,
#         "lang": lang,
#     }

#     print("📡 Broadcasting translation:", payload)
#     await manager.broadcast(payload)
#     return {"message": "Broadcasted"}

# # backend/app/main.py
# from dotenv import load_dotenv
# load_dotenv()

# from fastapi import FastAPI, WebSocket, WebSocketDisconnect
# import asyncio, json
# from app.socket_manager import manager
# from app.deepgram_session import connect_to_deepgram
# from app.utils.translate import translate_text   # <-- async
# from app.commit import Committer                 # see commit class below

# app = FastAPI()

# @app.websocket("/ws/stt/deepgram")
# async def ws_stt_deepgram(websocket: WebSocket):
#     await websocket.accept()
#     try:
#         dg = await connect_to_deepgram()
#     except Exception as e:
#         await websocket.send_json({"type":"error","message":f"Deepgram connect failed: {e}"})
#         await websocket.close()
#         return

#     agg = SentenceAggregator(max_wait_ms=2200, require_punct=False)
#     seq = 0
#     closed = asyncio.Event()

#     async def from_client_to_deepgram():
#         try:
#             while True:
#                 msg = await websocket.receive()
#                 if msg["type"] == "websocket.disconnect":
#                     try: await dg.close()
#                     except: pass
#                     break
#                 if (b := msg.get("bytes")):
#                     await dg.send(b)  # raw PCM 16-bit 48kHz
#                 elif (t := msg.get("text")):
#                     try:
#                         payload = json.loads(t)
#                         if payload.get("type") == "finalize":
#                             await dg.send(json.dumps({"type":"CloseStream"}))
#                     except:
#                         pass
#         finally:
#             closed.set()

#     async def from_deepgram_to_server():
#         try:
#             async for raw in dg:
#                 try:
#                     evt = json.loads(raw)
#                 except Exception:
#                     continue

#                 if evt.get("type") != "Results":
#                     continue

#                 ch = evt.get("channel") or {}
#                 alts = ch.get("alternatives") or []
#                 if not alts:
#                     continue

#                 best = alts[0]
#                 transcript = (best.get("transcript") or "").strip()
#                 is_final = bool(evt.get("is_final"))
#                 speech_final = bool(evt.get("speech_final"))

#                 # Debug: see DG flow
#                 print(f"[DG] is_final={is_final} speech_final={speech_final} tr='{transcript}'")

#                 # show partials in the textbox
#                 if transcript and not is_final:
#                     try: await websocket.send_json({"type":"stt.partial","text":transcript})
#                     except: pass

#                 # aggregate → sentence
#                 to_emit = agg.ingest(transcript, is_final=is_final, speech_final=speech_final)
#                 if not to_emit:
#                     continue

#                 seq += 1
#                 src_text = to_emit
#                 commit_seq = seq
#                 print(f"[DG] SENTENCE seq={commit_seq} '{src_text}'")

#                 # translate (await!)
#                 translated = await translate_text(src_text, "ko", "en")
#                 print(f"[TX] seq={commit_seq} -> '{translated}'")

#                 # New shape (your hook supports)
#                 live_msg_new = {
#                     "mode": "live",
#                     "text": translated,
#                     "seq": commit_seq,
#                     "src": {"text": src_text, "lang": "ko"},
#                     "tgt": {"lang": "en"},
#                 }
#                 # Legacy shape (also supported by your hook)
#                 live_msg_legacy = {
#                     "type": "translation",
#                     "payload": translated,
#                     "lang": "en",
#                     "meta": {
#                         "mode": "realtime",
#                         "partial": False,
#                         "segment_id": commit_seq,
#                         "rev": 0,
#                         "seq": commit_seq,
#                     },
#                 }

#                 # Send back to producer socket (optional UI feedback)
#                 try:
#                     await websocket.send_json(live_msg_new)
#                     await websocket.send_json(live_msg_legacy)
#                 except Exception as e:
#                     print("[DG] send back to producer failed:", e)

#                 # Broadcast to all /ws/translate listeners
#                 try:
#                     await manager.broadcast(live_msg_new)
#                     await manager.broadcast(live_msg_legacy)
#                     print(f"[BROADCAST] seq={commit_seq} '{translated[:60]}'")
#                 except Exception as e:
#                     print("[DG] broadcast error:", e)
#         finally:
#             closed.set()

#     consumer = asyncio.create_task(from_client_to_deepgram())
#     producer = asyncio.create_task(from_deepgram_to_server())
#     await closed.wait()
#     try:
#         consumer.cancel(); producer.cancel()
#     except: pass

# # ✅ mount the WebSocket router at the very end
# # backend/app/main.py
# @app.get("/debug/broadcast")
# async def debug_broadcast():
#     msg = {"mode":"live","text":"TEST BROADCAST","seq":0}
#     await manager.broadcast(msg)
#     return {"ok": True}

# app.include_router(router)

# backend/app/main.py
import os, json, asyncio, logging
from typing import Optional
from dotenv import load_dotenv
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

load_dotenv()

# --- local modules (single import each) ---
from app.socket_manager import manager
from app.deepgram_session import connect_to_deepgram
from app.utils.translate import translate_text  # async wrapper you already have
from app.routes import translate as translate_routes  # your existing REST routes

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
@app.websocket("/ws/stt/deepgram")
async def ws_stt_deepgram(websocket: WebSocket):
    await websocket.accept()
    try:
        dg = await connect_to_deepgram()  # <-- dg is created here
    except Exception as e:
        await websocket.send_json({"type": "error", "message": f"Deepgram connect failed: {e}"})
        await websocket.close()
        return

    seq = 0
    closed = asyncio.Event()

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
                            await dg.send(json.dumps({"type": "CloseStream"}))
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
        COMMIT_WAIT_MS = 500

        pending_kr: str | None = None
        pending_task: asyncio.Task | None = None

        def ends_like_sentence(t: str) -> bool:
            t = (t or "").rstrip()
            return bool(t) and t[-1] in SENTENCE_PUNCT

        def norm_ws(s: str) -> str:
            return " ".join((s or "").split())

        async def commit_now(kr_text: str):
            nonlocal seq, pending_kr, pending_task
            if not kr_text or not kr_text.strip():
                return
            # de-dup repeated finals
            if norm_ws(kr_text) == norm_ws(getattr(commit_now, "_last_kr", "")):
                return
            setattr(commit_now, "_last_kr", kr_text)

            seq += 1
            src_text = kr_text
            try:
                en = await translate_text(src_text, "ko", "en")
            except Exception as e:
                print("[TX] error:", e)
                en = src_text  # fail-open

            print(f"[A] FINAL seq={seq} KR='{src_text}' → EN='{en}'")

            # shape your client already supports
            live_msg_new = {
                "mode": "live",
                "text": en,
                "seq": seq,
                "src": {"text": src_text, "lang": "ko"},
                "tgt": {"lang": "en"},
            }
            live_msg_legacy = {
                "type": "translation",
                "payload": en,
                "lang": "en",
                "meta": {
                    "mode": "realtime",
                    "partial": False,
                    "segment_id": seq,
                    "rev": 0,
                    "seq": seq,
                },
            }

            try:
                await websocket.send_json(live_msg_new)
                await websocket.send_json(live_msg_legacy)
            except Exception as e:
                print("[DG] send back to producer failed:", e)

            try:
                await manager.broadcast(live_msg_new)
                await manager.broadcast(live_msg_legacy)
                print(f"[BROADCAST] seq={seq} '{en[:60]}'")
            except Exception as e:
                print("[DG] broadcast error:", e)

            pending_kr = None
            if pending_task and not pending_task.done():
                pending_task.cancel()
            pending_task = None

        async def arm_timer():
            nonlocal pending_task
            if pending_task and not pending_task.done():
                pending_task.cancel()

            async def _wait_and_commit(snap: str):
                try:
                    await asyncio.sleep(COMMIT_WAIT_MS / 1000.0)
                    if pending_kr and norm_ws(pending_kr) == norm_ws(snap):
                        await commit_now(pending_kr)
                except asyncio.CancelledError:
                    pass

            pending_task = asyncio.create_task(_wait_and_commit(pending_kr or ""))

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
                    pending_kr = transcript

                print(f"[DG][A] final: speech_final={speech_final} KR='{pending_kr or ''}'")

                if speech_final and pending_kr:
                    await commit_now(pending_kr)
                    continue

                if pending_kr and ends_like_sentence(pending_kr):
                    await commit_now(pending_kr)
                    continue

                if pending_kr:
                    await arm_timer()

        finally:
            # best-effort flush on shutdown
            if pending_kr:
                try:
                    await commit_now(pending_kr)
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

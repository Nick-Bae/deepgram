# Codex Context (real-time-translation)

How to use:
- At the start of a new Codex session, open this file, select the whole thing, and prompt: "Use this context for this session." Update it when project rules change.
- Do not paste secrets/keys into prompts. Keep .env files local only.

Project map:
- Frontend: Next.js 15 (`frontend/`), main UI component `components/TranslationBox.tsx` (clause buffering, spacing display, TTS queue, WS consumption). `utils/useTranslationSocket` for WS. Tailwind 4.
- Backend: FastAPI (`backend/app`), WS endpoint `/ws/translate?role=producer|viewer`. Translation pipeline in `app/utils/translate.py`; spacing helper `app/utils/spacing.py`; logging `app/data/translation_examples.jsonl`. TTS endpoints in backend consumed by frontend.
- Dev scripts (run inside `frontend/`):
  - `npm run dev-all` → update env, start backend (uvicorn) and frontend dev servers, then status + QR.
  - `npm run dev` → frontend only. `npm run backend` → backend only (uses `venv`).

Translation style & safety (must-follow):
- Audience: church/worship; keep tone pastoral, warm, and wholesome. No slang, no profanity, no flirtatious/suggestive phrasing.
- When Korean clauses imply congregation actions (일어나/일어나서/자리에서 일어나), translate as invitations: "let's stand" / "please stand," not singular past tense.
- If Korean lacks spaces, mentally restore natural spacing before translating; preserve meaning, not literal spacing.
- Keep Scripture references/names accurate. Prefer Biblical names map (`bible_names.json`) when present. Do not invent verses; translate only provided text.
- Split overly long sentences into clear, short ones suitable for live reading/listening.
- Keep ambiguity level: don’t add details that aren’t in the Korean; don’t dramatize emotions.
- If input is partial/abrupt, translate only what’s there; do not complete it.

Backend translation pipeline notes:
- Model: `OPENAI_MODEL` (default `gpt-4o-mini`), via `AsyncOpenAI` in `translate.py`.
- Few-shot: `_load_fewshot_examples` pulls up to 4 examples per lang pair from `app/data/translation_examples.jsonl`, preferring corrected ones.
- Glossary: `THEOLOGICAL_TERMS` for key terms; Bible names from `bible_names.json` when available.
- Spacing/normalization: `_preprocess_source_text` and `apply_ko_spacing` fix common Korean STT gaps before translation.
- Subject guardrails: prevents incorrect first-person; keeps congregation tone when markers appear.
- Logging: `_log_translation_example` appends to `translation_examples.jsonl` (fields: timestamp, langs, stt_text, auto_translation, final_translation, corrected flag).

translation_examples.jsonl hygiene:
- Runtime uses only the latest few entries (prefers corrected). File can grow; rotate occasionally: keep last 200–500 lines for speed/quality.
- Export helper: `python -m app.utils.export_translation_examples --source ko --target en --max 6` (writes `app/data/fewshot_examples.json`).

Frontend key behaviors:
- Displays live source with Korean word-segmentation via `formatSourceForDisplay` (Intl.Segmenter). Logs still show raw (unspaced) partials for debugging.
- Clause buffering: Deepgram partials accumulate in `clauseRef`; soft-final fallback triggers TTS enqueue when stable.
- WS final messages update `translated` and optionally enqueue TTS if not muted.
- TTS: supports Google and Gemini voices; queue managed via `enqueueFinalTTS`, `flushTTSQueue`; audio unlocked on interaction.
- Latency: `latencyMs` computed from last source update; reset when final WS message handled.

Environment expectations:
- Env vars loaded via dotenv. Core: `OPENAI_API_KEY`, `OPENAI_MODEL`, API_URL for frontend to reach backend (set by `utils/detectBackend.js`).
- Backend venv: `backend/venv` activated by scripts; dependencies in `backend/requirements.txt`.

Operational tips:
- When adding new translation rules, update this file and the backend prompt (`_build_system_prompt`) to keep them in sync.
- If translations drift, inspect recent rows in `translation_examples.jsonl`; prune bad ones and regenerate few-shot exports.
- Avoid committing large logs or secrets; add new log rotations if file size grows.

Copy/paste block for quick session start:
"Use the attached project context. Key rules: church-safe tone, no suggestive language, restore Korean spacing, keep scripture accurate, invite congregation on stand-up cues, split long sentences, translate only given text, prefer corrected few-shots from translation_examples.jsonl." 

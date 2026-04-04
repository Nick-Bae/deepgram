# Gap Analysis: deepgram-keyterms

**Match Rate: 96% → 100% (after fix applied)**

---

## Scores by Phase

| Phase | Score | Status |
|---|:---:|:---:|
| Phase 1: Config Cleanup | 100% | ✅ |
| Phase 2: Per-Org Keyterms (Backend) | 94% → 100% | ✅ |
| Phase 3: Sermon Vocab Merge | 100% | ✅ |
| Phase 4: Frontend UI | 94% | ✅ |
| **Overall** | **96% → 100%** | **✅** |

---

## Gap Found (1) — Fixed

| Gap | File | Fix Applied |
|---|---|---|
| `SttKeytermsPayload.keyterms` missing `max_length=50` on Pydantic Field | `routes/stt_keyterms.py:19` | Added `max_length=50` ✅ |

The store enforces the 50-term cap at the data layer, but without the Pydantic constraint the API would accept oversized payloads and silently truncate them. Now the API rejects them at validation time with a 422.

---

## Phase Verification

### Phase 1 — Config Cleanup ✅
- `DEFAULT_DEEPGRAM_KEYWORDS` restructured to `_WORSHIP_TERMS` (30) + `_BIBLE_BOOKS` (66) = 96 total
- All boost values removed (nova-3 doesn't support them)
- `DG_KEYWORDS_LIMIT` default raised 60 → 100
- `DEEPGRAM_KEYWORDS` env var in `.env` cleared (was overriding with old boost-format list)

### Phase 2 — Per-Org Keyterms ✅
- `STT_KEYTERMS_ROLES`, `STT_KEYTERMS_MAX` constants added to `multichurch_store.py:116-117`
- 3 methods added to `InMemoryMultiChurchStore`: `get_org_stt_keyterms`, `set_org_stt_keyterms`, `get_org_stt_keyterms_for_session`
- Same 3 methods added to `FirestoreMultiChurchStore` with direct Firestore writes (`org_ref.set(..., merge=True)`)
- `routes/stt_keyterms.py` created with `GET/PUT /api/org/{org_id}/stt-keyterms`
- Router mounted in `main.py` alongside other routes

### Phase 3 — Sermon Vocab Merge ✅
- `_build_keyterm_list()` helper added to `deepgram_session.py` with 3-tier priority (org_custom > sermon_vocab > defaults)
- Boost-aware dedup: strips `:boost` suffix when building dedup key
- `main.py` session start now calls `get_org_stt_keyterms_for_session()` + `script_store.get_vocab_set()` before `connect_to_deepgram()`
- Debug logging included when `DEEPGRAM_DEBUG=1`

### Phase 4 — Frontend UI ✅
- `frontend/lib/sttKeyterms.ts` — API client with typed response and `encodeURIComponent` for org ID
- `frontend/components/SttKeytermsEditor.tsx` — tag chip UI, slot counter, tier display (custom/sermon/default), save confirmation
- `frontend/pages/host/c/[churchSlug].tsx` — `SttKeytermsEditorLazy` wrapper + section rendered only for owner/admin

---

## Non-Breaking Differences from Design

| Item | Design | Implementation | Notes |
|---|---|---|---|
| API client path | `lib/api/sttKeyterms.ts` | `lib/sttKeyterms.ts` | No `lib/api/` folder exists in this project |
| API base import | `getApiBase()` | `API_URL` | `getApiBase()` doesn't exist; `API_URL` is the project pattern |
| `_build_keyterm_list` import | Inline in handler | Top-level import | Cleaner, follows Python conventions |

---

## Enhancements Beyond Design

- `SttKeytermsResponse` TypeScript type for better type safety
- `SttKeytermsEditorLazy` wrapper to defer token fetching until settings tab is shown
- Boost-stripping dedup handles legacy env var entries transparently

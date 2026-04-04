# Plan: deepgram-keyterms

## Executive Summary

| Perspective | Detail |
|---|---|
| **Problem** | Deepgram's keyterm feature is configured with a static 117-term list trimmed to 60 due to a low limit, boost values are silently ignored for nova-3, no per-church custom terms exist, and sermon script vocabulary is never fed back to Deepgram — meaning STT can still mishear words that are right in the sermon prep. |
| **Solution** | Fix nova-3 config correctness, raise the keyterm limit to 100, add per-org custom keyterms stored in Firestore, and dynamically inject sermon script vocabulary into Deepgram at session start — merged and prioritized across three tiers. |
| **Function / UX Effect** | Korean STT correctly hears pastor names, church-specific terms, and sermon-specific vocabulary. Hosts no longer need to mentally correct words that Deepgram consistently mishears during a service. |
| **Core Value** | STT accuracy becomes church-specific and sermon-aware — the right words are recognized before they even reach the translation step. |

---

## 1. Current System Inventory

### 1.1 How Deepgram Keyterms Work

Deepgram provides two mechanisms to bias STT toward specific vocabulary:

| API Param | Model Support | Behavior |
|---|---|---|
| `keywords=term:boost` | nova-2, enhanced, base | Boost (1–10) multiplies recognition probability for that term |
| `keyterm=term` | nova-3 | Binary hint — no boost, just prioritizes the term |

**Important**: These are sent as query params at connection time. They **cannot be updated mid-session** — a new Deepgram WebSocket connection is needed to change them.

### 1.2 Current Implementation

| File | What It Does |
|---|---|
| `backend/app/config/deepgram_keywords.py` | 117-item static list of Korean church/bible terms with boost values (e.g. `예수님:2.5`) |
| `backend/app/deepgram_session.py` | Reads list, applies `DG_KEYWORDS_LIMIT=60`, sends as `keywords` (nova-2) or `keyterm` (nova-3) |
| `backend/app/main.py` | Passes `keywords=None` for Korean sessions → uses defaults; `dg_keywords=[]` for non-Korean |

### 1.3 Current Gaps

| Gap | Impact |
|---|---|
| `DG_KEYWORDS_LIMIT=60` but nova-3 supports 100 | 57 terms silently dropped — mostly Bible book names |
| Boost values in config are meaningless for nova-3 | Config file looks like it matters but half the data is ignored |
| No per-org custom terms | Pastor names, church name, series titles never reach Deepgram |
| Sermon script vocab goes to OpenAI only | Words clearly visible in sermon prep can still be misheard by STT |
| `connect_to_deepgram()` called before sermon/org context loads | Even if we had dynamic terms, the connection is made too early |

---

## 2. Solution Design

### 2.1 Keyterm Priority Tiers

When building the keyterm list for a session, merge in this priority order (highest first):

```
Tier 1: Org custom keyterms (Firestore)     ← e.g. "Pastor Kim", "Bethany Church"
Tier 2: Sermon script vocab (script_store)  ← extracted Korean nouns from today's sermon
Tier 3: Default church/bible terms          ← current DEFAULT_DEEPGRAM_KEYWORDS (cleaned up)
```

Total cap: **100 keyterms** (nova-3 limit). Higher tiers consume slots first.

### 2.2 Default List Cleanup (nova-3 correctness)

- Remove boost values from `DEFAULT_DEEPGRAM_KEYWORDS` — they are silently ignored for nova-3 and create false confidence that they work
- Restructure into two groups for clarity:
  - **Core worship terms** (~30): 예수님, 하나님, 성령님, etc.
  - **Bible book names** (66): 창세기 through 요한계시록
- Total: 96 — fits within nova-3's 100-keyterm limit without trimming

### 2.3 Per-Org Custom Keyterms in Firestore

- Add `sttKeyterms: string[]` field to `organizations/{orgId}` document
- Backend reads this via `multichurch_store.get_org_stt_keyterms(org_id)`
- New API endpoint: `GET/PUT /api/org/stt-keyterms`
- Firestore rules: org owner/admin can read and write this field

### 2.4 Sermon Script → Deepgram Keyterms

At Deepgram session start, extract candidate keyterms from sermon:

1. Call `script_store.get_vocab_set(room_id=room_id)` — already returns Korean vocabulary tokens
2. Filter to tokens 2–8 chars (proper nouns, theological terms)
3. Fill remaining slots in the 100-term budget after Tier 1 is reserved

### 2.5 Connection Timing Fix

Currently `connect_to_deepgram()` is called before org context is resolved in the Deepgram WebSocket handler (`/ws/stt_deepgram`). The fix:
- Resolve `org_id` from the join message context before connecting
- Load Tier 1 (org custom) + Tier 2 (sermon vocab) before calling `connect_to_deepgram()`

### 2.6 Admin UI — STT Keywords Page

Add a "STT Keywords" section in the host console settings:

- Show active keyterms grouped by tier (default / church custom / sermon)
- Input field to add/remove church-specific terms (pastor name, series title, etc.)
- Real-time save to Firestore via API
- Warning if total exceeds 100

---

## 3. Implementation Scope

### Phase 1 — Config correctness (backend only, no user impact)
- [ ] Remove boost values from `DEFAULT_DEEPGRAM_KEYWORDS`
- [ ] Raise `DG_KEYWORDS_LIMIT` default to 100
- [ ] Add debug log showing how many keyterms are sent per session

### Phase 2 — Per-org custom keyterms (backend + Firestore)
- [ ] Add `sttKeyterms` field to org document in `multichurch_store.py`
- [ ] Add `GET /api/org/stt-keyterms` and `PUT /api/org/stt-keyterms` endpoints
- [ ] Merge org custom keyterms into `connect_to_deepgram()` call
- [ ] Update Firestore rules to allow owner/admin to read/write `sttKeyterms`

### Phase 3 — Sermon vocab → Deepgram (backend only)
- [ ] After org context resolves in `/ws/stt_deepgram`, load `script_store.get_vocab_set()`
- [ ] Filter vocab tokens to 2–8 chars
- [ ] Pass as Tier 2 in merged keyterm list

### Phase 4 — Admin UI
- [ ] Add STT Keywords section to church settings page in host console
- [ ] Display active keyterms by tier
- [ ] Add/remove custom terms with Firestore save
- [ ] Show slot usage (e.g. "72 / 100 keyterms active")

---

## 4. Files to Change

| File | Change |
|---|---|
| `backend/app/config/deepgram_keywords.py` | Remove boosts, restructure into two groups |
| `backend/app/deepgram_session.py` | Raise limit to 100, update `_qs()` comments |
| `backend/app/services/multichurch_store.py` | Add `get_org_stt_keyterms()` + `set_org_stt_keyterms()` |
| `backend/app/routes/org.py` (or similar) | Add `GET/PUT /api/org/stt-keyterms` |
| `backend/app/main.py` | Load and merge all 3 tiers before `connect_to_deepgram()` |
| `backend/firestore/firestore.rules` | Allow owner/admin to r/w `sttKeyterms` field |
| `frontend/pages/host/` (settings page) | Add STT Keywords management UI |

---

## 5. Out of Scope

- Mid-session keyterm reload (Deepgram connection is immutable per session — would require reconnect with audio gap)
- Boost values for nova-3 (not supported by Deepgram)
- Non-Korean languages (keyterm biasing is disabled for non-Korean sessions intentionally)
- Automatic term discovery / ML-based keyword extraction

---

## 6. Risks

| Risk | Mitigation |
|---|---|
| nova-3 keyterm limit not exactly 100 | Deepgram docs state "up to 100" — keep `DG_KEYWORDS_LIMIT` as a configurable env var |
| Sermon vocab extraction produces noise tokens | Apply min 2 char + hangul-only filter; existing `get_vocab_set()` already does this |
| Org admins add too many custom terms | UI shows slot usage counter; API caps at 50 custom terms (Tier 1 budget) |
| Deepgram changes keyterm API | Abstract keyterm building into `_build_keyterm_list()` helper for easy updates |

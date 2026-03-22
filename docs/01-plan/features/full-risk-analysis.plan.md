# Plan: full-risk-analysis

## Executive Summary

| Perspective | Detail |
|---|---|
| **Problem** | A production risk audit identified 14 failure scenarios spanning WebSocket lifecycle, Firestore state sync, billing integrity, and session security — three of which cause real user-facing data loss or security issues today. |
| **Solution** | Fix the three high-impact issues (Deepgram connection leak, silent segment save failure, stale session on logout) and add safeguards for billing mid-broadcast cap enforcement and room-end race conditions. |
| **Function / UX Effect** | Hosts can broadcast reliably even after Deepgram drops. Downloaded transcripts are complete. Users switching accounts land in the correct org. Billing caps are enforced during live broadcasts. |
| **Core Value** | A production-grade SaaS platform must not lose data silently, leak connections, or expose one user's context to another — fixing these closes the gap between MVP and production-ready. |

---

## 1. Risk Inventory (From Audit)

| # | Risk | Severity | Currently Handled | Fix Priority |
|---|------|----------|-------------------|--------------|
| R1 | Stale live room after backend restart | High | ✅ Fixed (startup cleanup) | Done |
| R2 | End Service button stuck (stale roomId) | High | ✅ Fixed (queryRoomId fallback) | Done |
| R3 | **Deepgram connection leak on mid-stream failure** | Medium | No | P1 |
| R4 | **Segment save silent failure (fire-and-forget)** | Medium | No | P1 |
| R5 | **Session storage not cleared on logout** | Medium | No | P1 |
| R6 | Billing hard cap not enforced mid-broadcast | Medium | No | P2 |
| R7 | Room sweeper + manual end race (double billing) | Medium | Partial | P2 |
| R8 | Billing period rollover double-count | High | Partial | P3 |
| R9 | WebSocket orphan connections (dead refs) | High | Partial | P3 |
| R10 | Per-IP connection limit race condition | High | No | P3 (DoS scenario) |
| R11 | Listener peak count best-effort | Low | Acknowledged | Accept |
| R12 | Frontend retry without backoff | Low | Partial | Accept |
| R13 | Trial countdown timer drift | Low | Partial | Accept |
| R14 | Prompt override exception masking | Low | Partial | Accept |

---

## 2. P1 Fixes — Implement Now

### R3: Deepgram Connection Leak

**What happens:**
- Host connects → Deepgram WebSocket opens → Deepgram drops mid-stream → backend `from_deepgram_to_server` task keeps running, TCP connection stays open
- After several failures, Deepgram's connection limit is hit → host can no longer broadcast until backend restarts

**Fix:**
- In `ws_stt_deepgram` finally block: explicitly close the `dg` connection object and cancel the background task
- Add a `closed` event signal that triggers cleanup of the Deepgram socket when the host WebSocket disconnects

**Files:** `backend/app/main.py` (ws_stt_deepgram handler, finally block)

---

### R4: Segment Save Silent Failure

**What happens:**
- Final translation is broadcast to listeners and fire-and-forget saved to Firestore via `run_in_executor`
- If Firestore is temporarily unavailable, the segment is lost with no error log, no retry
- Downloaded transcript has gaps with no explanation

**Fix:**
- Wrap the segment save executor call with error logging: if it fails, log `[SEGMENT_SAVE] failed seq=N org=X`
- Add a simple in-memory buffer: accumulate failed segments and retry once on the next successful segment save
- Do not block the broadcast path — keep it fire-and-forget but with visible failure logging

**Files:** `backend/app/main.py` (handle_commit, segment save executor call)

---

### R5: Session Storage Not Cleared on Logout

**What happens:**
- `persistStreamContext()` stores `orgId`, `roomId`, `serviceKey`, `churchSlug` in session storage
- On logout, only `clearAuthToken()` is called — session storage context remains
- Next user logs in on the same browser → gets redirected to previous user's org/room

**Fix:**
- In the logout handler: call `clearRoomInSession()` and clear the full stream context from session storage
- On login/auth change: clear stale context before applying new membership context

**Files:** `frontend/pages/host/c/[churchSlug].tsx` (logout handler, auth change effect)

---

## 3. P2 Fixes — Next Sprint

### R6: Billing Hard Cap Not Enforced Mid-Broadcast

**What happens:**
- `start_service()` checks `hardCapReached` before opening a room
- But once live, per-translation calls have no cap check
- A service that runs 10 minutes past the cap silently accumulates overage

**Fix:**
- In `handle_commit()` (the per-utterance translation handler): check `hardCapReached` flag from the org's in-memory state
- If cap is exceeded: skip translation, send a `CAP_REACHED` status message to the host WebSocket, close the room
- The room sweeper already handles trial expiry — extend the same pattern to monthly caps

**Files:** `backend/app/main.py` (handle_commit), `backend/app/services/multichurch_store.py` (hardCapReached flag)

---

### R7: Room Sweeper + Manual End Race (Double Billing)

**What happens:**
- Room sweeper detects idle room → calls `end_room()`
- Host simultaneously clicks End Service → also calls `end_room()`
- Both transactions read status="live" before either commits → both execute billing writes → minutes double-counted

**Fix:**
- The Firestore `end_room()` transaction already checks `status == "ended"` before proceeding — verify this read is inside the transaction boundary (not before it)
- Add explicit `alreadyEnded` check before billing writes, not just before the status update
- Log `[END_ROOM] duplicate end detected` when `alreadyEnded` is true, to surface if this happens in production

**Files:** `backend/app/services/multichurch_store.py` (end_room Firestore transaction, lines ~4414-4491)

---

## 4. P3 Fixes — Backlog

### R8: Billing Period Rollover Race
- Wrap `_roll_billing_period_if_needed()` + the usage write in a single Firestore transaction
- Currently two separate writes; midnight-boundary race can double-count or lose a minute

### R9: WebSocket Orphan Connections
- Add a periodic ping/pong health check to the connection manager
- Dead connections that don't respond to ping within 10s → `disconnect()` proactively

### R10: Per-IP Rate Limit Race
- Wrap the per-IP read-increment-write in an asyncio Lock (true critical section)
- Currently: lock protects the dict access but not the full read-modify-write cycle

---

## 5. Acceptance Criteria

### P1 (R3 — Deepgram leak)
- [ ] R3-1: When host WebSocket disconnects, Deepgram TCP connection closes within 2 seconds
- [ ] R3-2: Background task `from_deepgram_to_server` is cancelled on disconnect
- [ ] R3-3: No Deepgram connection limit errors after 10 consecutive connect/disconnect cycles in local testing

### P1 (R4 — Segment save)
- [ ] R4-1: Failed segment saves log `[SEGMENT_SAVE] failed` with seq, org, room IDs
- [ ] R4-2: Segments that fail are retried on the next successful save
- [ ] R4-3: Broadcast path is not blocked by save failure (fire-and-forget preserved)

### P1 (R5 — Session storage)
- [ ] R5-1: After logout, session storage contains no orgId, roomId, serviceKey
- [ ] R5-2: Logging in as a different user navigates to the new user's default org, not the previous user's
- [ ] R5-3: Existing single-user flow is unchanged

### P2 (R6 — Mid-broadcast cap)
- [ ] R6-1: When `hardCapReached=true`, new utterances are not translated
- [ ] R6-2: Host receives a clear WebSocket message explaining the cap was reached
- [ ] R6-3: Room is auto-ended when cap is hit mid-broadcast

### P2 (R7 — Double billing)
- [ ] R7-1: Calling `end_room()` twice for the same room results in exactly one billing write
- [ ] R7-2: Duplicate end is logged as a warning in backend output

---

## 6. Implementation Order

```
Phase 1 (P1 — ~2-3 hours):
  R5 → R4 → R3
  (session first: safest, no backend changes)
  (segment logging second: additive, low risk)
  (Deepgram cleanup third: WebSocket handler change)

Phase 2 (P2 — ~3-4 hours):
  R7 → R6
  (verify transaction boundary first, then add cap enforcement)

Phase 3 (P3 — Backlog):
  R8 → R9 → R10
  (require more testing, lower user-facing urgency)
```

---

## 7. Out of Scope

- Re-architecting the Firestore billing model (R8 full fix requires a transaction redesign)
- Adding retry UI for the host (R10 — backoff logic adds complexity for a rare edge case)
- Fixing listener peak count accuracy (acknowledged limitation of Firestore's lack of `max` atomic primitive)

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-03-21 | Initial plan from production risk audit |

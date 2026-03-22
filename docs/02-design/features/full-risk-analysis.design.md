# Design: full-risk-analysis

> **Plan**: [full-risk-analysis.plan.md](../01-plan/features/full-risk-analysis.plan.md)
> **Date**: 2026-03-21

---

## 1. Scope

Phase 1 (P1) fixes only — three issues with direct user-facing impact.
Phase 2 (P2) fixes included as lower-priority items.

| ID | Fix | Phase | Files |
|----|-----|-------|-------|
| R3 | Deepgram connection not closed after task cancel | P1 | `backend/app/main.py` |
| R4 | Segment save failure — logging exists, retry missing | P1 | `backend/app/main.py` |
| R5 | Session storage not cleared on logout | P1 | `frontend/pages/host/c/[churchSlug].tsx` |
| R6 | Billing hard cap not enforced mid-broadcast | P2 | `backend/app/main.py` |
| R7 | Double billing on simultaneous room end | P2 | `backend/app/services/multichurch_store.py` |

---

## 2. Current State Analysis

### R3 — Deepgram Connection Leak

**Current code (`main.py:1882-1889`):**
```python
consumer = asyncio.create_task(from_client_to_deepgram())
producer = asyncio.create_task(from_deepgram_to_server())
await closed.wait()
try:
    consumer.cancel()
    producer.cancel()
except:
    pass
```

**Problem:** `consumer.cancel()` and `producer.cancel()` send a cancellation signal, but:
1. `dg` (the Deepgram WebSocket object) is **never explicitly closed** after task cancellation
2. `from_client_to_deepgram()` only calls `await dg.close()` on `WebSocketDisconnect` — if Deepgram closes unexpectedly (Deepgram-side drop), `dg.close()` is never reached
3. Cancelled tasks do not await — the TCP connection may linger until the OS timeout (minutes)

**Evidence:** `dg.close()` appears only at `main.py:1375` inside `from_client_to_deepgram()`, not in the outer cleanup path.

---

### R4 — Segment Save Silent Failure

**Current code (`main.py:52-74`):**
```python
def _safe_append_segment(...) -> None:
    try:
        multichurch_store.append_translation_segment(...)
    except Exception as exc:
        print(f"[SEG] Failed to save segment org={org_id} room={room_id} seq={seq}: {exc}")
```

**Finding:** Logging IS already present. The gap is:
1. No retry — a transient Firestore error drops the segment permanently
2. The `run_in_executor` call at `main.py:1078` does not await the result or check it
3. There is no in-memory buffer to accumulate failed segments for retry

**Note:** This is less severe than initially assessed — failures are at least visible in logs. The real risk is transient Firestore outages during a live service where the pastor is speaking.

---

### R5 — Session Storage Not Cleared on Logout

**Current logout handler (`main.py:2030-2034`):**
```tsx
onClick={async () => {
  clearHostToken();
  clearAuthToken();
  await logout();
}}
```

**`clearStreamContext()` already exists in `utils/streamContext.ts:95-101`:**
```typescript
export function clearStreamContext(): void {
    window.sessionStorage.removeItem(STORAGE_KEYS.orgId);
    window.sessionStorage.removeItem(STORAGE_KEYS.roomId);
    window.sessionStorage.removeItem(STORAGE_KEYS.serviceKey);
    window.sessionStorage.removeItem(STORAGE_KEYS.churchSlug);
}
```

**Problem:** `clearStreamContext()` is never called on logout. It IS imported (line 35). The fix is a one-line addition to the logout handler.

---

### R6 — Mid-Broadcast Billing Cap

**Current flow:**
- `start_service()` checks `hardCapReached` before opening a room ✅
- `handle_commit()` (per-utterance handler) has **no cap check** — translates and broadcasts regardless

**`hardCapReached` is already stored on the org Firestore document** and read by `stale_live_rooms()` / `enforce_live_usage_caps()`. The room sweeper handles trial/monthly cap enforcement by ending the room — but only on its sweep interval (≥15s delay). A fast speaker can get 3-4 extra sentences translated after the cap is hit.

---

### R7 — Double Billing on Simultaneous End

**Current `end_room` Firestore transaction (multichurch_store.py ~4414-4491):**
- Transaction reads the room document
- Checks `if room.get("status") == "ended": return {"alreadyEnded": True}`
- If not ended: updates status, writes billing

**Problem:** The `alreadyEnded` guard IS inside the transaction. Firestore transactions serialize at the document level, so two concurrent `end_room()` calls on the same room document ARE protected — the second will see `status="ended"` and short-circuit. This risk is **lower than assessed** — Firestore transactions prevent the double-write.

**Remaining gap:** The `alreadyEnded: True` return is checked at `main.py:690` with `continue` but **not logged**. If a double-end happens, it's invisible in logs.

---

## 3. Implementation Design

### R3 — Fix: Explicit Deepgram Cleanup

**Location:** `backend/app/main.py`, after `consumer.cancel()` / `producer.cancel()`

**Change:** After cancelling both tasks, explicitly await their completion and close `dg`:

```python
# AFTER (replace the current cleanup block at ~line 1885-1889)
await closed.wait()
consumer.cancel()
producer.cancel()
try:
    await asyncio.gather(consumer, producer, return_exceptions=True)
except Exception:
    pass
try:
    await dg.close()
except Exception:
    pass
```

**Why `gather` with `return_exceptions=True`:** Awaiting cancelled tasks raises `CancelledError` — `return_exceptions=True` absorbs it without propagating. This ensures `dg.close()` is always reached.

**No new dependencies.** No behavior change for the happy path.

---

### R4 — Fix: Segment Save Retry Buffer

**Location:** `backend/app/main.py`

**Design:** Add a module-level `_failed_segments` deque (max 50 items) and a retry flush on the next successful save.

```python
# Module level
import collections
_failed_segments: collections.deque = collections.deque(maxlen=50)

def _safe_append_segment(org_id, room_id, seq, korean_text, english_text,
                          mode, match_score, timestamp) -> None:
    # Flush any previously failed segments first
    while _failed_segments:
        args = _failed_segments[0]
        try:
            multichurch_store.append_translation_segment(*args)
            _failed_segments.popleft()
        except Exception:
            break  # Still failing — stop and try again next time

    try:
        multichurch_store.append_translation_segment(
            org_id, room_id, seq=seq, korean_text=korean_text,
            english_text=english_text, mode=mode,
            match_score=match_score, timestamp=timestamp,
        )
    except Exception as exc:
        print(f"[SEG] Failed to save segment org={org_id} room={room_id} seq={seq}: {exc}")
        _failed_segments.append(
            (org_id, room_id, seq, korean_text, english_text, mode, match_score, timestamp)
        )
```

**Properties:**
- Max 50 buffered segments (~25 minutes of dense speech) — bounded memory
- Retries automatically on the next segment save — no separate task needed
- Broadcast path is never blocked (still fire-and-forget via `run_in_executor`)
- Also fix deprecated `asyncio.get_event_loop()` → `asyncio.get_running_loop()` at `main.py:1078`

---

### R5 — Fix: Clear Stream Context on Logout

**Location:** `frontend/pages/host/c/[churchSlug].tsx`, logout button `onClick`

**Change:** Add `clearStreamContext()` call (function already imported at line 35):

```tsx
// AFTER
onClick={async () => {
  clearStreamContext();   // ← add this line
  clearHostToken();
  clearAuthToken();
  await logout();
}}
```

**`clearStreamContext()` removes:** `orgId`, `roomId`, `serviceKey`, `churchSlug` from `sessionStorage`.

One line. Zero risk to existing flow.

---

### R6 — Fix: Cap Check in handle_commit (P2)

**Location:** `backend/app/main.py`, inside `handle_commit()` before translation call

**Design:** Read `hardCapReached` from org state. If true, send a warning to the host WebSocket and skip translation.

```python
# Inside handle_commit(), before the translate_text() call
_org_state = multichurch_store.get_org_live_state(target_org_id)
if _org_state and _org_state.get("hardCapReached"):
    await ws.send_json({
        "type": "CAP_REACHED",
        "message": "Monthly translation limit reached. Service will end shortly."
    })
    # Trigger room end (fire-and-forget)
    asyncio.create_task(_end_room_on_cap(target_org_id, target_room_id))
    return
```

**New helper `_end_room_on_cap`:**
```python
async def _end_room_on_cap(org_id: str, room_id: str) -> None:
    try:
        multichurch_store.end_room(org_id, room_id, reason="monthly_limit_reached")
    except Exception:
        pass
```

**Note:** `get_org_live_state()` must be a lightweight in-memory read (not a Firestore call per utterance). Verify this method exists and is cached; if not, use the existing `hardCapReached` flag from the service data cached during `refreshServices()`.

---

### R7 — Fix: Log Double-End Events (P2)

**Location:** `backend/app/main.py`, room sweeper loop (`main.py:690`)

**Change:** Add a log line when `alreadyEnded` is detected:

```python
result = multichurch_store.end_room(org_id, room_id, reason=reason)
if result.get("alreadyEnded"):
    print(f"[ROOM_SWEEPER] room already ended org={org_id} room={room_id} — no double billing")
    continue
```

This makes the double-end scenario visible in production logs without any behavior change. The Firestore transaction already prevents double billing.

---

## 4. Acceptance Criteria

### R3 — Deepgram Cleanup
| AC | Requirement |
|----|-------------|
| R3-1 | After host WebSocket disconnects, `dg.close()` is called within the finally path |
| R3-2 | Both `consumer` and `producer` tasks are awaited (with `return_exceptions=True`) before `dg.close()` |
| R3-3 | No `except: pass` swallowing — cleanup exceptions are silent but `dg.close()` always runs |

### R4 — Segment Retry Buffer
| AC | Requirement |
|----|-------------|
| R4-1 | `_failed_segments` deque exists at module level with `maxlen=50` |
| R4-2 | Failed segments are retried on next successful save call |
| R4-3 | Retry loop breaks on first retry failure (no infinite retry) |
| R4-4 | `asyncio.get_event_loop()` replaced with `asyncio.get_running_loop()` at both call sites |

### R5 — Session Clear on Logout
| AC | Requirement |
|----|-------------|
| R5-1 | `clearStreamContext()` called in logout `onClick` handler |
| R5-2 | After logout, `sessionStorage` contains no `orgId`, `roomId`, `serviceKey`, `churchSlug` |
| R5-3 | Existing logged-in flow unaffected |

### R6 — Mid-Broadcast Cap (P2)
| AC | Requirement |
|----|-------------|
| R6-1 | `handle_commit()` checks `hardCapReached` before calling translate |
| R6-2 | Host receives `CAP_REACHED` WebSocket message when cap is hit |
| R6-3 | Cap check uses cached/in-memory state — not a Firestore read per utterance |

### R7 — Double-End Logging (P2)
| AC | Requirement |
|----|-------------|
| R7-1 | `alreadyEnded` result from `end_room()` produces a `[ROOM_SWEEPER]` log line |
| R7-2 | No behavior change — Firestore transaction already prevents double billing |

---

## 5. Implementation Order

```
1. R5  → frontend/pages/host/c/[churchSlug].tsx   (1 line, safest)
2. R4  → backend/app/main.py                       (module-level deque + retry in _safe_append_segment)
3. R3  → backend/app/main.py                       (cleanup block after closed.wait())
4. R7  → backend/app/main.py                       (1 log line in sweeper)
5. R6  → backend/app/main.py                       (cap check in handle_commit — needs get_org_live_state verification)
```

---

## 6. Files Changed

| File | Changes |
|------|---------|
| `backend/app/main.py` | R3: Deepgram cleanup block; R4: `_failed_segments` deque + retry; R6: cap check in `handle_commit`; R7: log `alreadyEnded` |
| `frontend/pages/host/c/[churchSlug].tsx` | R5: `clearStreamContext()` in logout handler |

No new dependencies. No schema changes. No new API endpoints.

---

## Version History

| Version | Date | Changes |
|---------|------|---------|
| 1.0 | 2026-03-21 | Initial design from plan |

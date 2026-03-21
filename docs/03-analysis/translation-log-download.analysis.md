# Gap Analysis: translation-log-download

**Date**: 2026-03-21
**Match Rate**: 95% (42/44 items)
**Status**: ✅ PASS (≥90%)

---

## Summary

| Layer | Items | Matches | Rate |
|-------|:-----:|:-------:|:----:|
| Firestore Rules | 2 | 2 | 100% |
| `multichurch_store.py` | 14 | 14 | 100% |
| `main.py` | 6 | 6 | 100% |
| `routes/multichurch.py` | 10 | 10 | 100% |
| `[churchSlug].tsx` | 12 | 10 | 83% |
| **Total** | **44** | **42** | **95%** |

---

## Gaps Found

### GAP-1 (Medium): Button visibility shows during live sessions

**Design spec** (`design.md` §4.1.3):
```tsx
{svc.lastRoomId && svc.roomStatus !== "live" && ( <button ... /> )}
```
**Implementation** (`[churchSlug].tsx` line ~2611):
```tsx
const downloadRoomId = row.activeRoomId || row.lastRoomId || null;
{downloadRoomId ? ( <button ... /> )}
```
Button appears during live sessions via `activeRoomId` fallback — contradicts acceptance criterion "button only visible when room is NOT live."

**Fix**: Use `lastRoomId` only and add `!isLive` guard.

### GAP-2 (Low): Button label abbreviated

**Design**: `"Download Translation Log"`
**Implementation**: `"Download Log"`

Minor cosmetic deviation, acceptable if intentional.

---

## Acceptance Criteria (11/12 met)

| # | Criterion | Status |
|---|-----------|:------:|
| 1 | Final segments written to `rooms/{roomId}/segments/{seq}` | ✅ |
| 2 | Partial segments never written | ✅ |
| 3 | `mode` reflects `"live"`, `"pre"`, `"scripture"` | ✅ |
| 4 | `matchScore` only for `"pre"` mode | ✅ |
| 5 | Segment write failure does not interrupt broadcast | ✅ |
| 6 | `GET .../segments/export` returns valid CSV | ✅ |
| 7 | CSV rows ordered by `seq` | ✅ |
| 8 | `viewer` role blocked (403) | ✅ |
| 9 | `list_services` includes `lastRoomId` | ✅ |
| 10 | Button appears only when room is NOT live | ❌ (GAP-1) |
| 11 | Button shows loading state | ✅ |
| 12 | Empty sessions return CSV with header only | ✅ |

# Track 1 rollout — production observation evidence

Sanitized gate evidence for the staged validation in
`docs/01-plan/features/resource-cleanup-track-1.plan.md` §4. Identifiers
(org IDs, room IDs, instance IDs, raw log bodies) are kept out of this
document by design — see `security_log.py` guidance. Only bounded-enum
counts and derived statistics appear here.

## Deployed configuration during observation

| Item | Value |
|------|------:|
| Cloud Run service | `worshiptranslate-backend` (us-central1) |
| Revision serving 100% traffic | `worshiptranslate-backend-00158-h4s` |
| `ROOM_RECONCILER_ENABLED` | `1` |
| `ROOM_RECONCILER_INTERVAL_SEC` | default `30` (deploy workflow does not override) |
| `REDIS_ENABLED` | `0` (Track 1 constraint) |
| `--max-instances` | `1` (Track 1 constraint) |
| A4 recovery-rate alert | disabled (no baseline yet) |

## Gate 1 — 24-hour enabled observation

**Window:** `2026-09-19T03:24:15Z` → `2026-09-20T03:24:03Z` (24.00 h span).

### Tick heartbeat and cadence

| Metric | Value | Threshold | Pass |
|--------|------:|-----------|:---:|
| Total `reconciler_tick` events | 2,449 | steady heartbeat | ✓ |
| Expected at 30 s interval | ~2,880 | — | — |
| Mean inter-tick interval | 35.29 s | — | — |
| Inter-tick gap p50 | 30.3 s | ~30 s configured | ✓ |
| Inter-tick gap p95 | 44.50 s | — | — |
| Inter-tick gap p99 | 46.04 s | — | — |
| Inter-tick gap max | 49.91 s | < 900 s (A1) | ✓ |
| Gaps > 45 s | 65 | — | — |
| Gaps > 60 s | **0** | 0 | ✓ |
| Gaps > 900 s (A1 threshold) | **0** | 0 | ✓ |

Observed mean (35.3 s) exceeds the configured 30 s interval. The
timing is consistent with a *sleep-after-work* loop (each tick's own
duration was 0–1.1 s in this window) and variable Cloud Run CPU
scheduling during idle periods. All 65 elevated gaps sat in a narrow
45–50 s band, and no gap exceeded 49.9 s — well under the A1
15-minute absence threshold.

### Instance appearance

Three instance IDs appeared in the window. One served 97% of it
(2,381 of 2,449 ticks over 23.36 h). During a handover at 04:02–04:03
UTC, the timestamps show two instances ticking in parallel for about
62 s before Cloud Run converged on the long-serving instance; no
overlap was observed at any other point. The 40 s boundary gap on
either side of that handover is included in the global gap statistics
above; the max within-instance gap is 49.9 s.

Cloud Run's `--max-instances=1` setting is a steady-state concurrency
limit, not an absolute-uniqueness guarantee — brief overlap during
revision or instance transition is expected behavior. Track 1's
resource-cleanup design is safe under this pattern (`REDIS_ENABLED=0`
means each instance's local socket map is private, and the reconciler
gates cleanup on Firestore `status == "ended"` reads).

### Tick outcomes and telemetry

| Metric | Value | Threshold | Pass |
|--------|------:|-----------|:---:|
| Ticks with `outcome=ok` | 2,449 / 2,449 | 100% | ✓ |
| Ticks with `outcome=firestore_error` \| `loop_error` \| `cleanup_error` | 0 | 0 | ✓ |
| Ticks with `overdue=true` | 0 | 0 or returns to 0 | ✓ |
| Max `terminal_rooms_with_resources` | 0 | 0 or returns to 0 | ✓ |
| Max `oldest_overdue_cleanup_seconds` | 0.0 s | p99 < 60 s | ✓ |
| Tick `duration_seconds` p50 / p95 / max | 0 s / 0.29 s / 1.10 s | no long stalls | ✓ |

### Recovery actions and cleanup correlation

| Metric | Value |
|--------|------:|
| `reconciler_action{reason=ended_room_local_cleanup}` | **1** |
| `reconciler_action{reason=cleanup_error}` | 0 |
| `reconciler_diagnostic{kind=cleanup_started}` | 1 |
| `reconciler_diagnostic{kind=cleanup_finished, outcome=ok}` | 1 |

The single `ended_room_local_cleanup` action correlates 1:1 with a
matching `cleanup_started` / `cleanup_finished{outcome=ok}` pair. Wall
time from start → finish → action emit: ~37 ms.

### Ticks advanced while resources existed

Sampled the 20-minute window around the cleanup event. The reconciler
ticked at 30–31 s cadence continuously through the resource-active
service (host produced audio, then disconnected, then room ended, then
reconciler cleaned local state) — no gap in that window exceeded 32 s.

### No reconciler-induced false termination

Structural guarantee: the reconciler emits `ended_room_local_cleanup`
only after reading Firestore `status == "ended"`. It never itself
writes terminal state (see `test_on_shutdown_structural.py` and the
`multichurch_store` audit trail). The single cleanup action was
authorized by Firestore `status == "ended"` and followed an observed
host WebSocket disconnect (`[DG] reason=browser_disconnect`) 3 m 38 s
earlier. This is not evidence of reconciler-induced false termination.

### Gate 1 verdict

All four reviewer criteria met:

- ✓ No inter-tick gap exceeded the A1 15-minute absence threshold (max 49.9 s).
- ✓ Ticks advanced whenever local WebSocket resources existed (30–31 s cadence in resource-active windows).
- ✓ No cleanup was unmatched or overdue (0 overdue ticks; single action 1:1 with cleanup lifecycle diagnostics).
- ✓ **Firestore audit correlation captured.** The single cleanup event's room document was read via a read-only Firestore GET (script committed to no repo; identifiers kept private). Sanitized result: `status='ended'`, `endReason='host_end'`, `initiatorMetadata='unavailable'`, `endedAt` was 3.07 s before the reconciler's cleanup action (within the acceptance window of 0 s ≤ Δ ≤ 60 s). `host_end` is the End Service endpoint's default reason — the route is authenticated. Individual initiator UID is not stored on the room record by design, hence `initiatorMetadata='unavailable'`. No evidence of reconciler-induced false termination.

**Gate 1: PASSED.**

## Gate 2 — Controlled deploy reconnect evidence

**Pending.** Requires triggering a real backend redeploy while a host
and listener are actively connected, then recording that both
transparently reconnect (`1012` transient close, `/resolve` polling,
no `terminated` tombstone). Best scheduled during a low-traffic window
or paired with the next production release.

## Gate 3 — 7-day soak

**Pending gate 2.** Track 1 exit criteria per §4 of the plan require
all five staged-validation steps green across a 7-day soak.

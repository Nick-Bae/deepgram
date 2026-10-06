# Observability — Scope

**Status:** scope + failing-before tests ONLY. **No application code. No implementation.** Branch stays local, unpushed, implementation-free. Implementation waits for separate authorization.
**Branch:** `prep/observability` off `origin/main` HEAD `d7473ed7`.
**Source of truth:** `origin/main` @ `d7473ed7`. All implementation analysis and source references use the current `origin/main` tree. **No archive, ZIP, or `tmp/` reference is permitted.**
**Context:** Gate 3 was **TERMINATED EARLY and closed INCIDENT DETECTED / NOT PASSED** on 2026-10-05 — before the full 7-day observation window would have elapsed. The 2026-10-04 Gemini Live GoAway storm, the 65-minute CPU pin, and the recovery traffic shift already disqualified the gate; closure did not wait for 10:00 AM CDT 2026-10-05. See `~/gemini-live-incident-2026-10-04/GATE-3-CLOSURE.md` and `cpu-investigation/UNRESOLVED-REPORT.md § Observability`.

## Why

The 2026-10-04 CPU-pin incident could not be diagnosed from existing logs because no in-process signal was emitted when the event loop stopped making progress. Cloud Monitoring showed CPU and concurrency; it did not show event-loop lag, `asyncio.all_tasks()` growth, or per-call write latencies. The next pin — whether or not it's the same root cause — must leave a trail.

## What ships (under separate authorization)

Four metric sources, all emitted as single-line JSON on stdout, same wire shape as `room_reconciler.py:_emit()` so they feed existing Cloud Logging → log-based metrics without new infrastructure.

### M1 — event-loop lag
- Background task sampling `asyncio.sleep(0)` wall-clock latency every 1 s, maintained in a 60-sample ring.
- Emission every 30 s: `{"event":"event_loop_lag","schema_version":"1","component":"event_loop","instance_id":"<id>","p50_ms":...,"p95_ms":...,"p99_ms":...,"max_ms":...,"samples":60}`.
- Severity: `INFO` normally; `WARNING` when p99 ≥ 100 ms; `ERROR` when p99 ≥ 500 ms.

### M2 — asyncio task count
- Gauge of `len(asyncio.all_tasks())` sampled every 30 s.
- Emission: `{"event":"asyncio_task_count","schema_version":"1","component":"event_loop","instance_id":"<id>","count":<int>,"delta_30s":<int>}`.
- Severity: `INFO`; `WARNING` when `delta_30s` has been positive and ≥ 10/sample for 3 consecutive samples (unbounded growth).

### M3 — translation-example write duration
- `_log_translation_example` (`backend/app/utils/translate.py:432`) prints `[TX_LOG] write_ms=<float>` on every call (zero cost unless profiled).
- Not an event JSON — a plain text marker, grep-friendly, matched by a Cloud Logging log-based metric with a numeric extractor on `write_ms`.
- Separate concern from the Phase 1 fewshot-cache candidate (`investigation/cpu-pin-rc@94833263`); this scope is observability only.

### M4 — readiness-probe failure alert
- No code change — a Cloud Logging alert policy proposal only. Alert on 3 consecutive `startupProbe`/`livenessProbe` failures within 5 min on `worshiptranslate-backend`. Policy YAML lives in `ops/monitoring/alerts/` under a follow-up change.
- Documented here so no reviewer wonders why the test suite does not cover it.

## Non-goals

- No change to Cloud Run config (that's scope 2's territory).
- No change to the Deepgram or Gemini handlers (that's scope 3's territory).
- No change to `_log_translation_example`'s cache logic (that's `investigation/cpu-pin-rc@94833263`'s territory).
- No new infrastructure dependencies — reuses existing stdout → Cloud Logging path.

## Test shape

Three Python unit tests (included in this commit, failing on `d7473ed7`):

- `test_observability_event_loop_lag.py` — asserts a `start_event_loop_lag_sampler(emit_interval_s=0.1)` function exists, runs for ≥ 2 emission ticks against a cooperative event loop, emits events whose JSON parses and contains `p99_ms`.
- `test_observability_task_count.py` — asserts `start_task_count_sampler(emit_interval_s=0.1)` exists, emits events with integer `count` and signed-integer `delta_30s`.
- `test_observability_tx_log_duration.py` — invokes `_log_translation_example(...)` and asserts stdout contains a `[TX_LOG] write_ms=` substring with a parseable float.

## Pass criteria for implementation (future PR, under separate authorization)

- All three tests PASS.
- No regression in existing `backend/tests/` suite.
- Lag sampler adds < 0.5% CPU overhead at idle (measured in cpu_repro harness scenario B).
- Task-count sampler produces monotonic `count` under a controlled task-leak scenario and flat `count` at idle.
- Alert-policy YAML lands in `ops/monitoring/alerts/` as a separate commit on the same branch, reviewed independently.

## Sequencing

Fixed implementation order: **observability → liveness recovery → Gemini rotation → non-prod validation → prod deploy → fresh 7-day Gate 3 clock.** Scope 2 (liveness recovery) consumes M4 probe-fail alert; sequencing not optional.

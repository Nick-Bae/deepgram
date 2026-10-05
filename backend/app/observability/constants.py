"""Observability tunables — single source for intervals/thresholds/rate-limits.

Everything here is a module-level constant; no I/O, no logic. Changes to any
of these numbers should be reviewed as a config-only change — the samplers
themselves do not pin behaviour to specific values.
"""
from __future__ import annotations

# --- event-loop lag sampler -------------------------------------------------
LAG_EMIT_INTERVAL_S: float = 30.0        # emit summary every N seconds
LAG_SAMPLE_INTERVAL_S: float = 1.0       # take one `asyncio.sleep(0)` sample per N seconds
LAG_WINDOW_SAMPLES: int = 60             # ring buffer size (60 samples @ 1 s = 60 s window)
LAG_P99_WARN_MS: float = 100.0           # severity bumps to WARNING when p99 ≥ this
LAG_P99_ERROR_MS: float = 500.0          # severity bumps to ERROR when p99 ≥ this

# --- asyncio task-count sampler ---------------------------------------------
TASK_COUNT_EMIT_INTERVAL_S: float = 30.0
TASK_COUNT_WINDOW_SAMPLES: int = 2       # tracked for delta_30s (prev vs current)

# --- process CPU sampler ----------------------------------------------------
PROCESS_CPU_EMIT_INTERVAL_S: float = 30.0

# --- executor queue sampler -------------------------------------------------
EXECUTOR_QUEUE_EMIT_INTERVAL_S: float = 30.0

# --- heartbeat watchdog -----------------------------------------------------
HEARTBEAT_TICK_INTERVAL_S: float = 1.0           # async heartbeat bumps counter every N seconds
HEARTBEAT_WATCHDOG_CHECK_INTERVAL_S: float = 1.0 # thread checks counter every N seconds
HEARTBEAT_STALL_THRESHOLD_S: float = 5.0         # if counter hasn't advanced in this many seconds, the loop is stalled

STACK_CAPTURE_MIN_INTERVAL_S: float = 60.0       # at most one stack capture per minute
STACK_CAPTURE_HOUR_CAP: int = 3                  # at most this many stack captures per hour
STACK_CAPTURE_WINDOW_S: float = 3600.0           # sliding window for the hour cap

# --- stack-capture SIZE caps (review defect #2 remediation) -----------------
# Rate limiting controls FREQUENCY of captures; these constants bound the
# SIZE of each capture event so one pathological snapshot cannot overwhelm
# Cloud Logging.
STACK_CAPTURE_MAX_THREADS: int = 64             # cap on threads emitted per capture
STACK_CAPTURE_MAX_FRAMES_PER_THREAD: int = 128  # cap on frames emitted per thread
# Conservative Cloud Logging-safe aggregate serialized-bytes cap. Google Cloud
# Logging rejects log entries whose `textPayload`/`jsonPayload` exceed 256 KB
# per entry; we stop emitting at 192 KB (75% of ceiling) so one capture burst
# — one `stack_capture` header + N `stack_frames` lines — collectively stays
# well under the per-entry ceiling even accounting for ingestion overhead and
# the structured envelope that Cloud Logging wraps each line in.
STACK_CAPTURE_MAX_SERIALIZED_BYTES: int = 192_000

# Shared across watchdog recovery emissions. Reusing the stack-capture
# limits keeps a single logical cadence for watchdog-side rate limiting.
WATCHDOG_RECOVERY_MIN_INTERVAL_S: float = STACK_CAPTURE_MIN_INTERVAL_S
WATCHDOG_RECOVERY_HOUR_CAP: int = STACK_CAPTURE_HOUR_CAP
WATCHDOG_RECOVERY_WINDOW_S: float = STACK_CAPTURE_WINDOW_S

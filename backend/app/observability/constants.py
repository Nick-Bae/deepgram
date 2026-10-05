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

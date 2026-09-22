"""Shared allowlist, timeouts, JSON emit, clock, and exit-code
definitions for the Window 1 preflight helpers.

Both `count_live_rooms.py` and `roster_cross_check.py` import
from here so the two commands agree on:
  - which (project, database) pairs are permitted;
  - how to check the FIRESTORE_EMULATOR_HOST environment
    variable against the target type;
  - the exit-code scheme the runbook's decision tables branch on;
  - the "one JSON on stdout, human diagnostics on stderr" I/O
    contract;
  - the monotonic-deadline pattern used to bound overall
    wall-clock time across paginated / multi-request reads.

Reviewer's PR #40 pattern (allowlist + env-mismatch check) is
mirrored here so the helpers refuse a production write against
the emulator or an emulator read against production, before any
Google Cloud API is touched.
"""
from __future__ import annotations

import json
import os
import sys
import time
from enum import IntEnum
from typing import Any, NoReturn


# --- Exit codes (the runbook's decision tables branch on these) ------------


class ExitCode(IntEnum):
    OK = 0
    USAGE = 1
    ALLOWLIST_REFUSAL = 2
    MALFORMED = 3
    INCOMPLETE = 4
    STALE = 5
    PERMISSION = 6
    TIMEOUT = 7
    UNRESOLVED_MISMATCH = 8
    UPSTREAM_API = 9


# --- Allowlist -------------------------------------------------------------

# (project, database) tuples the helpers will read from. Production
# and emulator entries listed separately so the env-mismatch check
# can enforce which environment each is used with.
_PRODUCTION_TARGETS: set[tuple[str, str]] = {
    ("sturdy-dogfish-472313-k6", "worship-translation"),
}
_EMULATOR_TARGETS: set[tuple[str, str]] = {
    ("cleanup-track1-emulator", "(default)"),
}
ALLOWED_TARGETS: set[tuple[str, str]] = _PRODUCTION_TARGETS | _EMULATOR_TARGETS


class TargetRefused(Exception):
    """Raised when (project, database) is not on the allowlist."""


class EnvMismatch(Exception):
    """Raised when FIRESTORE_EMULATOR_HOST does not match the
    intended target type."""


def check_target(project: str, database: str, env: dict[str, str] | None = None) -> None:
    """Fail-closed target + environment validation.

    Raises `TargetRefused` if (project, database) is not on the
    allowlist. Raises `EnvMismatch` if `FIRESTORE_EMULATOR_HOST`
    is set for a production target OR unset for an emulator
    target. Callers translate these into ExitCode.ALLOWLIST_REFUSAL
    (rc=2) with the exception message as the reason field.
    """
    env = env if env is not None else dict(os.environ)
    target = (project, database)
    if target not in ALLOWED_TARGETS:
        raise TargetRefused(
            f"target {(project, database)!r} is not on the allowlist "
            f"({sorted(str(t) for t in ALLOWED_TARGETS)!r})"
        )
    emu = env.get("FIRESTORE_EMULATOR_HOST") or ""
    if target in _PRODUCTION_TARGETS and emu:
        raise EnvMismatch(
            f"target {target[0]!r}/{target[1]!r} is production but "
            f"FIRESTORE_EMULATOR_HOST is set — the Google client "
            f"would silently redirect reads to the emulator"
        )
    if target in _EMULATOR_TARGETS and not emu:
        raise EnvMismatch(
            f"target {target[0]!r}/{target[1]!r} is the emulator but "
            f"FIRESTORE_EMULATOR_HOST is not set — the Google client "
            f"would talk to real Firestore under an emulator project id"
        )


# --- Structured JSON emit --------------------------------------------------


def emit(payload: dict[str, Any]) -> None:
    """Emit exactly one structured JSON line on stdout. Human
    diagnostics go to stderr — never mix. Callers that need to
    exit non-zero use `die(rc, payload)` which composes emit +
    sys.exit."""
    sys.stdout.write(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
    )
    sys.stdout.write("\n")
    sys.stdout.flush()


def diag(msg: str) -> None:
    """Human-readable diagnostic on stderr. Never adds structure —
    the JSON contract belongs to stdout."""
    sys.stderr.write(msg + "\n")
    sys.stderr.flush()


def die(rc: ExitCode | int, payload: dict[str, Any]) -> NoReturn:
    """Emit `payload` on stdout, exit with `rc`. The runbook's
    decision tables branch on rc; the JSON payload carries the
    field the operator records in the audit trail."""
    emit(payload)
    sys.exit(int(rc))


# --- Monotonic deadline ----------------------------------------------------


class Deadline:
    """Wall-clock budget the caller enforces around paginated /
    multi-request reads. Per-RPC timeouts alone can be aggregated;
    an outer monotonic deadline keeps the whole operation bounded.

    Usage:
        d = Deadline(30.0)
        for page in iterator:
            if d.expired():
                die(ExitCode.TIMEOUT, {...})
            ...
    """

    def __init__(self, budget_seconds: float):
        if budget_seconds <= 0:
            raise ValueError(f"budget_seconds must be > 0, got {budget_seconds!r}")
        self._start = time.monotonic()
        self._budget = float(budget_seconds)

    def remaining(self) -> float:
        return max(0.0, self._budget - (time.monotonic() - self._start))

    def elapsed(self) -> float:
        return time.monotonic() - self._start

    def expired(self) -> bool:
        return self.remaining() <= 0.0

    def rpc_timeout(self, ceiling: float) -> float:
        """A per-RPC timeout that never exceeds the remaining
        budget. Callers pass their own ceiling (e.g. 10 s) and
        this shrinks it if the overall deadline is closer than
        that."""
        return max(0.0, min(float(ceiling), self.remaining()))


# --- Convenience: iso timestamp for audit fields --------------------------


def iso_utc_now() -> str:
    """`YYYY-MM-DDTHH:MM:SS.fffZ` — same format the adapter's
    `_emit` uses for `ts`. Suitable for `verified_at`
    audit-trail fields."""
    from datetime import datetime, timezone
    return (
        datetime.now(timezone.utc)
        .strftime("%Y-%m-%dT%H:%M:%S.") + f"{datetime.now(timezone.utc).microsecond // 1000:03d}Z"
    )

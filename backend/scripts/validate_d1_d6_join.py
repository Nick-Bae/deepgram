#!/usr/bin/env python3
"""Validate the host↔listener D1/D6 join against a captured probe/listener pair.

WARNING — DIAGNOSTIC OUTPUT, NOT F2 ACCEPTANCE
----------------------------------------------
Results below are DIAGNOSTIC. Supplying `--clock-skew-ms N` enables
PASS/FAIL verdicts under an operator-asserted skew bound, but does
NOT establish a valid acoustic-chunk origin. The `firstPostResetAudioMs`
mark anchors on the first audio chunk AFTER the previous probe reset
(typically silence, NOT acoustic onset). A valid F2 D6 measurement
requires `mark_first_interim()` at the Deepgram interim-result handler.
Until that mark exists, D1 and D6 remain INCONCLUSIVE under the F2
procedure regardless of the clock bound supplied.

Zero-network. Reads a `[LATENCY_PROBE]` log file and the listener harness
JSONL file; prints per-utterance records with distinct D1 and D6
metrics, plus summary statistics. Exits 0 on success. Intended for
offline analysis BEFORE proposing another production smoke.

Usage:

    python backend/scripts/validate_d1_d6_join.py \\
        --probe-log <path> \\
        --listener-jsonl <path>
"""
from __future__ import annotations

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
if os.path.join(ROOT, "backend") not in sys.path:
    sys.path.insert(0, os.path.join(ROOT, "backend"))

from app.analysis.latency_join import (  # noqa: E402
    DEFAULT_CLOCK_SKEW_MS,
    join_probe_and_listener,
    load_from_paths,
    summarize,
)


def _fmt_ms(x) -> str:
    return "-" if x is None else str(int(x))


def _fmt_verdict(x) -> str:
    if x is None:
        return "-"
    return {"PASS": "pass", "FAIL": "fail", "INCONCLUSIVE": "incon"}.get(x, str(x))


def _fmt_key(key) -> str:
    if not key:
        return "-"
    # v4 keys are (room_id, kind, val) 3-tuples. Also support v3 2-tuples
    # for any pre-v4 fixture that may be around.
    if len(key) == 3:
        rid, kind, val = key
        rid_short = (rid[:12] + "..") if rid and len(rid) > 14 else (rid or "-")
        return f"{rid_short}|{kind}={val}"
    if len(key) == 2:
        kind, val = key
        return f"{kind}={val}"
    return "-"


_WARNING_BANNER = (
    "WARNING: results below are diagnostic; "
    "F2 D1/D6 remain INCONCLUSIVE until mark_first_interim lands."
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(
            "DIAGNOSTIC: F2 D1/D6 join. Even with --clock-skew-ms set, "
            "this tool produces DIAGNOSTIC results, not F2-acceptance values. "
            "The D6 anchor is the first audio chunk AFTER the previous probe "
            "reset (typically silence); a valid acoustic origin requires "
            "mark_first_interim() instrumentation."
        ),
    )
    ap.add_argument("--probe-log", required=True)
    ap.add_argument("--listener-jsonl", required=True)
    ap.add_argument(
        "--clock-skew-ms",
        type=lambda s: None if s in ("", "none", "None") else int(s),
        default=DEFAULT_CLOCK_SKEW_MS,
        help=(
            "Explicit one-sided clock-skew bound in ms between host (Cloud "
            "Run wall-clock) and listener (laptop wall-clock). "
            "v4 DEFAULT = None: forces all D1/D6 verdicts to INCONCLUSIVE "
            "and flags each entry with no_clock_bound_measured. "
            "Pass an explicit integer (e.g. --clock-skew-ms 1000) to "
            "assert a bound and derive PASS/FAIL. The operator takes "
            "responsibility for the bound they assert."
        ),
    )
    args = ap.parse_args()

    probes, listeners = load_from_paths(args.probe_log, args.listener_jsonl)
    joined = join_probe_and_listener(
        probes, listeners, final_only=True, clock_skew_ms=args.clock_skew_ms
    )
    summary = summarize(joined)

    print(_WARNING_BANNER)
    print()
    skew_display = "None (INCONCLUSIVE forced)" if args.clock_skew_ms is None else f"{args.clock_skew_ms}"
    print(
        f"loaded probes={len(probes)}  listener_records={len(listeners)}  "
        f"joined_entries={len(joined)}  clock_skew_ms={skew_display}"
    )
    print()
    header = (
        f"{'key':30s}  "
        f"{'d1_ms(sf->recv)':>15s}  {'d1':>5s}  "
        f"{'d6_ms(audio->recv)':>18s}  {'d6':>5s}  "
        f"{'skew':>5s}  {'orphan':<14s}  {'flags':<24s}"
    )
    print(header)
    for j in joined:
        flags = []
        if j.no_clock_bound_measured:
            flags.append("no_bound")
        if j.impossible_negative_latency:
            flags.append("impossible_neg")
        if j.format_skew_warning:
            flags.append("fmt_skew")
        flag_str = ",".join(flags) if flags else "-"
        print(
            f"{_fmt_key(j.key):30s}  "
            f"{_fmt_ms(j.d1_end_to_end_ms):>15s}  {_fmt_verdict(j.d1_verdict):>5s}  "
            f"{_fmt_ms(j.d6_end_to_end_ms):>18s}  {_fmt_verdict(j.d6_verdict):>5s}  "
            f"{_fmt_ms(j.format_skew_ms):>5s}  {(j.orphan_reason or '-'):<14s}  {flag_str:<24s}"
        )
    print()
    print("summary:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

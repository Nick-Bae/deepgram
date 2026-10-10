"""Offline host↔listener latency correlation for D1 / D6 — DIAGNOSTIC ONLY.

================================================================
WARNING — DIAGNOSTIC OUTPUT, NOT F2 ACCEPTANCE
================================================================
Results produced by this join are DIAGNOSTIC. Supplying
`--clock-skew-ms N` enables PASS/FAIL verdicts under an
operator-asserted skew bound, but does NOT establish a valid
acoustic-chunk origin. The `firstPostResetAudioMs` mark (alias
for the legacy `audioChunkStartMs` / `t0Ms`) anchors on the
first audio chunk AFTER the previous probe reset — typically
silence, NOT acoustic onset. A valid F2 D6 measurement requires
`mark_first_interim()` (or equivalent) at the Deepgram
interim-result handler. Until that mark exists, D1 and D6
remain INCONCLUSIVE under the F2 procedure regardless of the
clock bound supplied.
================================================================

F2 follow-up v5. Joins host-side `[LATENCY_PROBE]` emits (with
`broadcastSeq` + `segmentId` + `firstPostResetAudioMs` +
`speechFinalMs`) to listener-side JSONL records (with `seq`,
`meta_segment_id`, receipt `ts`) and produces per-utterance records
that report D1 (speech_final → listener recv) and D6 (first audio
chunk → listener recv) separately. BOTH are diagnostic.

speech_final is a PROXY for the acoustic utterance end
-------------------------------------------------------

`speechFinalMs` is the wall-clock receipt time at the backend of
Deepgram's `speech_final=True` event. It is NOT the acoustic
utterance end. The acoustic end happens FIRST; `speech_final`
receipt happens LATER by Deepgram's endpointing lag (typically
100-800 ms, bounded by the configured endpointing threshold).
The correct formula:

    acoustic_end_to_listener = (listener_recv - speech_final_receipt)
                             + (speech_final_receipt - acoustic_end)

Both terms are ≥ 0. The measured latency "listener_recv -
speech_final_receipt" is therefore an UNDER-estimate of the true
acoustic-end-to-listener latency by exactly the Deepgram endpointing
lag. D1 per the procedure measures "within 10 s of utterance end";
measured against this proxy, the real-world latency to acoustic end
is LONGER by the endpointing lag. For boundary cases operators
should either widen the clock-skew bound to subsume that lag OR
classify near-threshold values as INCONCLUSIVE (see
"Measurement uncertainty policy" below).

Measurement uncertainty policy
------------------------------

Host timestamps come from the Cloud Run server wall-clock. Listener
timestamps come from the harness's local wall-clock. The default
`DEFAULT_CLOCK_SKEW_MS = None` forces ALL D1/D6 verdicts to
INCONCLUSIVE and flags every joined entry with
`no_clock_bound_measured`. Supplying `--clock-skew-ms N` lets an
operator ASSERT a bound and derive PASS/FAIL from it. That assertion
is the operator's responsibility; this tool does not measure the
offset.

The classification function is:

    uncertainty_band(result_ms, threshold_ms, skew_ms):
        if result_ms + skew_ms <  threshold_ms:  "PASS"
        if result_ms - skew_ms >  threshold_ms:  "FAIL"
        otherwise:                               "INCONCLUSIVE"

With skew_ms = 0 the classification is definitive on either side of
the threshold (equal to threshold → INCONCLUSIVE by convention
because both halves of the inequality are strict).

Expected input formats
----------------------

Host side (`parse_probe_log`): accepts either a line beginning with the
`[LATENCY_PROBE]` prefix followed by a JSON object, OR a bare JSON
object line. Keys recognised in payload:
    t0Ms              int, absolute epoch ms, utterance start
    audioChunkStartMs int, absolute epoch ms — EXPLICIT D6 anchor; if
                      absent, falls back to t0Ms
    speechFinalMs     int, absolute epoch ms — D1 anchor; if absent,
                      falls back to (t0Ms + utteranceEndMs)
    utteranceEndMs    int, delta from t0 (legacy alias for the above)
    utteranceEndAbsMs int, alt absolute form (legacy)
    broadcastSeq      int, primary join key
    segmentId         str, fallback join key
    engine/orgId/roomId

Listener side (`parse_listener_jsonl`): the harness file
`~/track1-redis-rollout-2026-10-06/f2-exec-prep/listener_capture.py`
writes JSONL events; `ws_message` records are the ones joined.
Required keys per record:
    event: "ws_message"
    ts    ISO-8601, millisecond precision; both ...Z and ...+00:00Z
          forms are accepted (the harness happens to emit the
          double-tz quirk)
    seq           optional, int or str (digits-only)
    meta_segment_id   optional, fallback join key
    meta_is_final / meta_partial  used to filter partials
    format            "modern" | "legacy" | "other"

Modern/legacy dedup
-------------------

Each logical broadcast on the backend fans out to BOTH a modern and
a legacy listener message. For D1/D6 we take the EARLIEST listener
receipt among the modern+legacy pair (first-arrival). A pair where
modern and legacy differ by more than 100 ms is flagged with
`format_skew_warning_ms`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterable, Literal, Optional

# v4: default is None (no measured bound). When clock_skew_ms is None,
# D1/D6 verdicts are forced INCONCLUSIVE with the flag
# `no_clock_bound_measured`. The CLI accepts --clock-skew-ms N to assert
# a bound explicitly; absent that assertion, the operator cannot derive
# PASS/FAIL from this instrumentation. See measurement-uncertainty-policy.md.
DEFAULT_CLOCK_SKEW_MS: Optional[int] = None

Verdict = Literal["PASS", "FAIL", "INCONCLUSIVE"]


def estimate_clock_skew_from_evidence(evidence_path: str) -> Optional[int]:
    """Stub for a future clock-skew measurement harness.

    Returns None in this build. A future implementation would read a
    pair of files emitted by a measurement run: (a) host-side sentinels
    with monotonic + wall-clock epoch pairs; (b) listener-side receipts
    of those sentinels with the laptop's wall clock. One-way-delay
    estimation under an NTP-floor assumption (both clocks within a few
    hundred ms of GPS truth) yields a bound. Until that harness exists,
    this returns None and callers must either (a) pass an explicit
    `--clock-skew-ms N` or (b) accept INCONCLUSIVE verdicts.
    """
    _ = evidence_path  # keep signature for future implementation
    return None


def uncertainty_band(result_ms: int, threshold_ms: int, skew_ms: int) -> Verdict:
    """Classify a measurement against a threshold under a one-sided skew bound.

    - PASS         if result_ms + skew_ms  <  threshold_ms   (fully below band)
    - FAIL         if result_ms - skew_ms  >  threshold_ms   (fully above band)
    - INCONCLUSIVE otherwise (band straddles the threshold)

    With skew_ms = 0, the inequalities are strict so result_ms == threshold_ms
    yields INCONCLUSIVE. Operators with no measured skew should keep the
    default conservative skew bound (1 000 ms) rather than passing 0.
    """
    if skew_ms < 0:
        skew_ms = -skew_ms
    if result_ms + skew_ms < threshold_ms:
        return "PASS"
    if result_ms - skew_ms > threshold_ms:
        return "FAIL"
    return "INCONCLUSIVE"


def _parse_ts_to_ms(ts: str) -> Optional[int]:
    """Parse an ISO-8601 timestamp (incl. the harness's `...+00:00Z` quirk) to ms epoch."""
    if not isinstance(ts, str) or not ts:
        return None
    raw = ts.strip()
    if not raw:
        return None
    if raw.endswith("+00:00Z"):
        raw = raw[:-1]
    elif raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


@dataclass
class ProbeRecord:
    engine: Optional[str]
    org_id: Optional[str]
    room_id: Optional[str]
    t0_ms: Optional[int]
    audio_chunk_start_abs_ms: Optional[int]   # D6 anchor
    speech_final_abs_ms: Optional[int]        # D1 anchor
    broadcast_seq: Optional[int]
    segment_id: Optional[str]

    @classmethod
    def from_payload(cls, d: dict) -> "ProbeRecord":
        t0 = d.get("t0Ms")
        t0_int = int(t0) if t0 is not None else None

        # v4: prefer the new semantic field name; fall back to v2/v3 alias.
        audio_abs = d.get("firstPostResetAudioMs")
        if audio_abs is None:
            audio_abs = d.get("audioChunkStartMs")
        if audio_abs is None and t0_int is not None:
            audio_abs = t0_int
        audio_abs_int = int(audio_abs) if audio_abs is not None else None

        sf_abs = d.get("speechFinalMs")
        if sf_abs is None:
            sf_abs = d.get("utteranceEndAbsMs")
        if sf_abs is None:
            u_delta = d.get("utteranceEndMs")
            if u_delta is not None and t0_int is not None:
                sf_abs = t0_int + int(u_delta)
        sf_abs_int = int(sf_abs) if sf_abs is not None else None

        bseq = d.get("broadcastSeq")
        bseq_int = int(bseq) if isinstance(bseq, int) else (
            int(bseq) if isinstance(bseq, str) and bseq.isdigit() else None
        )

        return cls(
            engine=d.get("engine"),
            org_id=d.get("orgId"),
            room_id=d.get("roomId"),
            t0_ms=t0_int,
            audio_chunk_start_abs_ms=audio_abs_int,
            speech_final_abs_ms=sf_abs_int,
            broadcast_seq=bseq_int,
            segment_id=str(d.get("segmentId")) if d.get("segmentId") is not None else None,
        )


@dataclass
class ListenerRecord:
    ts_ms: Optional[int]
    room_id: Optional[str]
    seq: Optional[int]
    segment_id: Optional[str]
    is_final: Optional[bool]
    partial: Optional[bool]
    format: Optional[str]

    @classmethod
    def from_jsonl_record(cls, d: dict, *, inherited_room_id: Optional[str] = None) -> Optional["ListenerRecord"]:
        if d.get("event") != "ws_message":
            return None
        seq_val = d.get("seq")
        if isinstance(seq_val, int):
            seq_int: Optional[int] = seq_val
        elif isinstance(seq_val, str) and seq_val.isdigit():
            seq_int = int(seq_val)
        else:
            seq_int = None
        meta_seg = d.get("meta_segment_id")
        # Prefer explicit room_id on the message row; otherwise fall back
        # to the room_id inherited from the preceding `resolved` or
        # `ws_open` event in the same recorder session. See
        # parse_listener_jsonl for how the context is maintained.
        rid = d.get("roomId") or d.get("room_id") or inherited_room_id
        return cls(
            ts_ms=_parse_ts_to_ms(d.get("ts") or ""),
            room_id=rid,
            seq=seq_int,
            segment_id=str(meta_seg) if meta_seg is not None else None,
            is_final=d.get("meta_is_final") if isinstance(d.get("meta_is_final"), bool) else None,
            partial=d.get("meta_partial") if isinstance(d.get("meta_partial"), bool) else None,
            format=d.get("format"),
        )


@dataclass
class JoinedUtterance:
    key: tuple
    probe: ProbeRecord
    listener_first_recv_ms: Optional[int]
    d1_end_to_end_ms: Optional[int]
    d1_verdict: Optional[Verdict]  # "PASS" | "FAIL" | "INCONCLUSIVE" | None(orphan)
    d6_end_to_end_ms: Optional[int]
    d6_verdict: Optional[Verdict]
    format_skew_ms: Optional[int]
    format_skew_warning: bool
    orphan_reason: Optional[str]
    # v4 flags
    no_clock_bound_measured: bool = False
    impossible_negative_latency: bool = False


_D1_THRESHOLD_MS = 10_000
_D6_THRESHOLD_MS = 15_000
_FORMAT_SKEW_WARN_MS = 100


def _probe_key(p: ProbeRecord) -> Optional[tuple]:
    """v4: scope by room to prevent cross-room collisions on identical seq."""
    rid = p.room_id or "<no-room>"
    if p.broadcast_seq is not None:
        return (rid, "seq", p.broadcast_seq)
    if p.segment_id:
        return (rid, "segment", p.segment_id)
    return None


def _listener_key(l: ListenerRecord) -> Optional[tuple]:
    """v4: scope by room to prevent cross-room collisions on identical seq."""
    rid = l.room_id or "<no-room>"
    if l.seq is not None:
        return (rid, "seq", l.seq)
    if l.segment_id:
        return (rid, "segment", l.segment_id)
    return None


def parse_probe_log(lines: Iterable[str]) -> list[ProbeRecord]:
    out: list[ProbeRecord] = []
    for raw in lines:
        if not raw:
            continue
        text = raw.strip()
        if not text:
            continue
        if "[LATENCY_PROBE]" in text:
            try:
                idx = text.index("{")
            except ValueError:
                continue
            payload_text = text[idx:]
        else:
            payload_text = text
        try:
            payload = json.loads(payload_text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        out.append(ProbeRecord.from_payload(payload))
    return out


def parse_listener_jsonl(lines: Iterable[str]) -> list[ListenerRecord]:
    """Parse a listener JSONL stream, inheriting room_id from `resolved` /
    `ws_open` context events to subsequent `ws_message` rows that omit it.

    The listener recorder emits events in order:
      {"event": "listener_capture_start", ...}
      {"event": "resolved", "roomId": "<id>", ...}
      {"event": "ws_open", "room_id": "<id>", ...}
      {"event": "ws_message", ...}        <- room_id not stamped per row
      {"event": "ws_message", ...}
      ...optionally a reconnect across rooms...
      {"event": "resolved", "roomId": "<other>", ...}
      {"event": "ws_message", ...}        <- now in the other room

    We track the current room_id from the latest context event and bind
    it onto each subsequent ws_message row that doesn't already carry one.
    """
    out: list[ListenerRecord] = []
    current_room_id: Optional[str] = None
    for raw in lines:
        if not raw:
            continue
        text = raw.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        evt = payload.get("event")
        if evt in ("resolved", "ws_open", "listener_capture_start"):
            # Update inherited context; some events use roomId, others room_id.
            rid = payload.get("roomId") or payload.get("room_id")
            if rid:
                current_room_id = str(rid)
            continue
        if evt == "ws_close":
            # A close doesn't by itself reset the room_id — a subsequent
            # resolved/ws_open will replace it. Leaving current_room_id
            # intact is correct for a transient recv timeout close.
            continue
        rec = ListenerRecord.from_jsonl_record(payload, inherited_room_id=current_room_id)
        if rec is not None:
            out.append(rec)
    return out


def _collect_recv_times_per_key(
    listeners: list[ListenerRecord], *, final_only: bool
) -> dict[tuple, dict[str, int]]:
    """Bucket listener receipt times keyed by `(room_id, kind, val)`.

    Each listener row contributes to TWO buckets:
      1. its scoped key (room, kind, val) — the authoritative key when the
         listener's room is known (either stamped on the row or inherited
         from a preceding `resolved`/`ws_open` event);
      2. an unscoped key ("<no-room>", kind, val) when AND ONLY WHEN the
         listener had NO room context at all. In production the recorder
         always emits `resolved` first, so this fallback is used by legacy
         tests / fixtures whose streams lack the context event.

    This split preserves the hard property that two DIFFERENT rooms with
    the same seq never cross-collide (both listener rows have distinct
    scoped keys), while remaining tolerant of context-less test fixtures.
    """
    bucket: dict[tuple, dict[str, int]] = {}
    for lrec in listeners:
        if final_only:
            if lrec.is_final is False:
                continue
            if lrec.partial is True:
                continue
        key = _listener_key(lrec)
        if key is None or lrec.ts_ms is None:
            continue
        fmt = (lrec.format or "other").lower()
        slot = bucket.setdefault(key, {})
        prev = slot.get(fmt)
        if prev is None or lrec.ts_ms < prev:
            slot[fmt] = lrec.ts_ms
    return bucket


def join_probe_and_listener(
    probes: list[ProbeRecord],
    listeners: list[ListenerRecord],
    *,
    final_only: bool = True,
    clock_skew_ms: Optional[int] = DEFAULT_CLOCK_SKEW_MS,
) -> list[JoinedUtterance]:
    """Scope joins by (room_id, broadcast_seq) OR (room_id, segment_id).

    v4 behavior:
    - When `clock_skew_ms is None`, D1/D6 verdicts are forced INCONCLUSIVE
      with `no_clock_bound_measured=True`. The CLI's `--clock-skew-ms N`
      asserts a bound explicitly.
    - A computed latency < -skew is physically impossible (listener recv
      before host emit) and is flagged `impossible_negative_latency=True`
      with verdict INCONCLUSIVE — NEVER classified PASS.
    """
    recv_by_key = _collect_recv_times_per_key(listeners, final_only=final_only)
    out: list[JoinedUtterance] = []
    joined_keys: set[tuple] = set()
    no_bound = clock_skew_ms is None
    effective_skew = 0 if no_bound else clock_skew_ms

    for p in probes:
        key = _probe_key(p)
        if key is None:
            out.append(JoinedUtterance(
                key=("unkeyed", id(p)),
                probe=p,
                listener_first_recv_ms=None,
                d1_end_to_end_ms=None, d1_verdict=None,
                d6_end_to_end_ms=None, d6_verdict=None,
                format_skew_ms=None, format_skew_warning=False,
                orphan_reason="no_host_mark",
                no_clock_bound_measured=no_bound,
            ))
            continue

        recv_times = recv_by_key.get(key)
        if not recv_times:
            out.append(JoinedUtterance(
                key=key, probe=p,
                listener_first_recv_ms=None,
                d1_end_to_end_ms=None, d1_verdict=None,
                d6_end_to_end_ms=None, d6_verdict=None,
                format_skew_ms=None, format_skew_warning=False,
                orphan_reason="no_listener",
                no_clock_bound_measured=no_bound,
            ))
            continue

        joined_keys.add(key)
        recv_first = min(recv_times.values())
        skew: Optional[int] = None
        skew_warn = False
        if "modern" in recv_times and "legacy" in recv_times:
            skew = abs(recv_times["modern"] - recv_times["legacy"])
            skew_warn = skew > _FORMAT_SKEW_WARN_MS

        impossible_negative = False

        if p.speech_final_abs_ms is None:
            d1 = None
            d1_verdict: Optional[Verdict] = None
        else:
            d1 = recv_first - p.speech_final_abs_ms
            if no_bound:
                d1_verdict = "INCONCLUSIVE"
            else:
                d1_verdict = uncertainty_band(d1, _D1_THRESHOLD_MS, effective_skew)
                # Impossible-negative: listener recv BEFORE host emit by
                # more than the asserted skew bound is physically impossible.
                if d1 < -effective_skew:
                    d1_verdict = "INCONCLUSIVE"
                    impossible_negative = True

        if p.audio_chunk_start_abs_ms is None:
            d6 = None
            d6_verdict: Optional[Verdict] = None
        else:
            d6 = recv_first - p.audio_chunk_start_abs_ms
            if no_bound:
                d6_verdict = "INCONCLUSIVE"
            else:
                d6_verdict = uncertainty_band(d6, _D6_THRESHOLD_MS, effective_skew)
                if d6 < -effective_skew:
                    d6_verdict = "INCONCLUSIVE"
                    impossible_negative = True

        out.append(JoinedUtterance(
            key=key, probe=p,
            listener_first_recv_ms=recv_first,
            d1_end_to_end_ms=d1, d1_verdict=d1_verdict,
            d6_end_to_end_ms=d6, d6_verdict=d6_verdict,
            format_skew_ms=skew, format_skew_warning=skew_warn,
            orphan_reason=None,
            no_clock_bound_measured=no_bound,
            impossible_negative_latency=impossible_negative,
        ))

    listener_only_keys: set[tuple] = set(recv_by_key.keys()) - joined_keys
    for key in listener_only_keys:
        recv_first = min(recv_by_key[key].values())
        # Reconstruct a sentinel probe for context — key is now (room, kind, val).
        rid, kind, val = key
        out.append(JoinedUtterance(
            key=key,
            probe=ProbeRecord(
                engine=None, org_id=None, room_id=(None if rid == "<no-room>" else rid),
                t0_ms=None, audio_chunk_start_abs_ms=None, speech_final_abs_ms=None,
                broadcast_seq=val if kind == "seq" else None,
                segment_id=val if kind == "segment" else None,
            ),
            listener_first_recv_ms=recv_first,
            d1_end_to_end_ms=None, d1_verdict=None,
            d6_end_to_end_ms=None, d6_verdict=None,
            format_skew_ms=None, format_skew_warning=False,
            orphan_reason="listener_only",
            no_clock_bound_measured=no_bound,
        ))

    return out


def _percentile(sorted_ms: list[int], p: int) -> Optional[int]:
    if not sorted_ms:
        return None
    k = max(0, min(len(sorted_ms) - 1, int(round((p / 100.0) * (len(sorted_ms) - 1)))))
    return sorted_ms[k]


def summarize(joined: list[JoinedUtterance]) -> dict:
    full_d1 = [j for j in joined if j.orphan_reason is None and j.d1_end_to_end_ms is not None]
    full_d6 = [j for j in joined if j.orphan_reason is None and j.d6_end_to_end_ms is not None]
    d1_sorted = sorted(j.d1_end_to_end_ms for j in full_d1 if j.d1_end_to_end_ms is not None)
    d6_sorted = sorted(j.d6_end_to_end_ms for j in full_d6 if j.d6_end_to_end_ms is not None)

    d1_pass_count = sum(1 for j in full_d1 if j.d1_verdict == "PASS")
    d1_fail_count = sum(1 for j in full_d1 if j.d1_verdict == "FAIL")
    d1_incon_count = sum(1 for j in full_d1 if j.d1_verdict == "INCONCLUSIVE")
    d6_pass_count = sum(1 for j in full_d6 if j.d6_verdict == "PASS")
    d6_fail_count = sum(1 for j in full_d6 if j.d6_verdict == "FAIL")
    d6_incon_count = sum(1 for j in full_d6 if j.d6_verdict == "INCONCLUSIVE")
    skew_warn_count = sum(1 for j in joined if j.orphan_reason is None and j.format_skew_warning)

    return {
        "n_probes": sum(1 for j in joined if j.orphan_reason in (None, "no_listener")),
        "n_joined": sum(1 for j in joined if j.orphan_reason is None),
        "n_impossible_negative_latency": sum(1 for j in joined if j.impossible_negative_latency),
        "n_no_clock_bound_measured": sum(1 for j in joined if j.no_clock_bound_measured),
        "d1_n": len(full_d1),
        "d1_pass_count": d1_pass_count,
        "d1_fail_count": d1_fail_count,
        "d1_inconclusive_count": d1_incon_count,
        "d1_pass_ratio": (d1_pass_count / len(full_d1)) if full_d1 else None,
        "d1_min_ms": d1_sorted[0] if d1_sorted else None,
        "d1_p50_ms": _percentile(d1_sorted, 50),
        "d1_p95_ms": _percentile(d1_sorted, 95),
        "d1_max_ms": d1_sorted[-1] if d1_sorted else None,
        "d6_n": len(full_d6),
        "d6_pass_count": d6_pass_count,
        "d6_fail_count": d6_fail_count,
        "d6_inconclusive_count": d6_incon_count,
        "d6_pass_ratio": (d6_pass_count / len(full_d6)) if full_d6 else None,
        "d6_min_ms": d6_sorted[0] if d6_sorted else None,
        "d6_p50_ms": _percentile(d6_sorted, 50),
        "d6_p95_ms": _percentile(d6_sorted, 95),
        "d6_max_ms": d6_sorted[-1] if d6_sorted else None,
        "n_format_skew_warnings": skew_warn_count,
        "n_orphan_no_listener": sum(1 for j in joined if j.orphan_reason == "no_listener"),
        "n_orphan_listener_only": sum(1 for j in joined if j.orphan_reason == "listener_only"),
        "n_unkeyed_probes": sum(1 for j in joined if j.orphan_reason == "no_host_mark"),
    }


def load_from_paths(probe_path: str, listener_path: str) -> tuple[list[ProbeRecord], list[ListenerRecord]]:
    with open(probe_path, "r", encoding="utf-8") as fh:
        probes = parse_probe_log(fh)
    with open(listener_path, "r", encoding="utf-8") as fh:
        listeners = parse_listener_jsonl(fh)
    return probes, listeners

"""F2 follow-up v2: offline host↔listener latency correlation.

Verifies D1 (speech_final → listener recv ≤ 10 s) and D6 (audio chunk
start → listener recv ≤ 15 s) as DISTINCT metrics. Includes a
discriminating fixture where D1 passes and D6 fails.
"""
from __future__ import annotations

import os

from app.analysis.latency_join import (
    DEFAULT_CLOCK_SKEW_MS,
    join_probe_and_listener,
    load_from_paths,
    parse_listener_jsonl,
    parse_probe_log,
    summarize,
    uncertainty_band,
)


FIXTURE_DIR = os.path.join(os.path.dirname(__file__), "fixtures")


def _by_key(joined, key):
    """Find a joined entry by key. Accepts either v3 2-tuple (kind, val)
    or v4 3-tuple (room, kind, val) for cross-version test fixtures."""
    for j in joined:
        if j.key == key:
            return j
        # v4: 3-tuple keys. Match by (kind, val) ignoring room.
        if len(key) == 2 and len(j.key) == 3 and j.key[1:] == key:
            return j
    raise AssertionError(f"key {key!r} not found. keys={[j.key for j in joined]}")


# v4: existing tests expect PASS/FAIL semantics under skew=1000. v4 default
# is None (INCONCLUSIVE). To preserve existing assertions as regression
# coverage for the uncertainty-band logic, call the join with explicit
# skew=1000. New tests cover the no-bound/default behavior separately.
_V4_LEGACY_SKEW_MS = 1_000


# ---- smoke fixture ----------------------------------------------------------


def test_fixture_round_trip_counts():
    probes, listeners = load_from_paths(
        os.path.join(FIXTURE_DIR, "f2_probe_log.txt"),
        os.path.join(FIXTURE_DIR, "f2_listener.jsonl"),
    )
    assert len(probes) == 5
    # 12 listener events total; 8 are ws_message (modern+legacy for seq 1/2/3/777)
    assert len(listeners) == 8


def test_fixture_join_computes_distinct_d1_and_d6():
    probes, listeners = load_from_paths(
        os.path.join(FIXTURE_DIR, "f2_probe_log.txt"),
        os.path.join(FIXTURE_DIR, "f2_listener.jsonl"),
    )
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)

    # seq=1 → audioChunkStart=1e12, speechFinal=1e12+2000, recv=1e12+2500
    j1 = _by_key(joined, ("seq", 1))
    assert j1.orphan_reason is None
    assert j1.d1_end_to_end_ms == 500 and j1.d1_verdict == "PASS"
    assert j1.d6_end_to_end_ms == 2500 and j1.d6_verdict == "PASS"

    # seq=2 → audioChunkStart=1e12+3000, speechFinal=1e12+5500, recv=1e12+6100
    j2 = _by_key(joined, ("seq", 2))
    assert j2.d1_end_to_end_ms == 600 and j2.d1_verdict == "PASS"
    assert j2.d6_end_to_end_ms == 3100 and j2.d6_verdict == "PASS"

    # seq=3 → audioChunkStart=1e12+6000, speechFinal=1e12+8000, recv=1e12+24000
    # d1 = 16000 > 10000 + 1000 skew → FAIL; d6 = 18000 > 15000 + 1000 skew → FAIL
    j3 = _by_key(joined, ("seq", 3))
    assert j3.d1_end_to_end_ms == 16000 and j3.d1_verdict == "FAIL"
    assert j3.d6_end_to_end_ms == 18000 and j3.d6_verdict == "FAIL"

    # seq=4 / seq=99 → no listener
    assert _by_key(joined, ("seq", 4)).orphan_reason == "no_listener"
    assert _by_key(joined, ("seq", 99)).orphan_reason == "no_listener"

    # seq=777 → listener-only
    assert _by_key(joined, ("seq", 777)).orphan_reason == "listener_only"


def test_fixture_summary_matches_known_truth():
    probes, listeners = load_from_paths(
        os.path.join(FIXTURE_DIR, "f2_probe_log.txt"),
        os.path.join(FIXTURE_DIR, "f2_listener.jsonl"),
    )
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    stats = summarize(joined)
    assert stats["n_probes"] == 5
    assert stats["n_joined"] == 3
    assert stats["d1_pass_count"] == 2
    assert abs(stats["d1_pass_ratio"] - (2 / 3)) < 1e-9
    assert stats["d1_min_ms"] == 500
    assert stats["d1_max_ms"] == 16000
    assert stats["d6_pass_count"] == 2
    assert abs(stats["d6_pass_ratio"] - (2 / 3)) < 1e-9
    assert stats["d6_min_ms"] == 2500
    assert stats["d6_max_ms"] == 18000
    assert stats["n_orphan_no_listener"] == 2
    assert stats["n_orphan_listener_only"] == 1
    assert stats["n_unkeyed_probes"] == 0


# ---- discriminating fixture: D1 PASS, D6 FAIL ------------------------------


def test_discriminator_d1_pass_d6_fail():
    """The validator must separate D1 and D6 — this fixture proves it.

    Operator held speech for 17 s (long utterance). The translation
    arrived 5 s after speech_final (D1 = 5 000 ms ≤ 10 000 ms PASS)
    but 22 s after the first audio chunk (D6 = 22 000 ms > 15 000 ms FAIL).
    """
    probes, listeners = load_from_paths(
        os.path.join(FIXTURE_DIR, "d1_pass_d6_fail_probe.txt"),
        os.path.join(FIXTURE_DIR, "d1_pass_d6_fail_listener.jsonl"),
    )
    assert len(probes) == 1
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    j = _by_key(joined, ("seq", 501))
    assert j.orphan_reason is None
    assert j.d1_end_to_end_ms == 5000
    assert j.d1_verdict == "PASS", "D1 must PASS: 5000 + 1000 skew < 10000 threshold"
    assert j.d6_end_to_end_ms == 22000
    assert j.d6_verdict == "FAIL", "D6 must FAIL: 22000 - 1000 skew > 15000 threshold"

    stats = summarize(joined)
    assert stats["d1_pass_count"] == 1 and stats["d6_pass_count"] == 0
    assert stats["d1_fail_count"] == 0 and stats["d6_fail_count"] == 1
    assert stats["d1_inconclusive_count"] == 0 and stats["d6_inconclusive_count"] == 0
    assert stats["d1_pass_ratio"] == 1.0 and stats["d6_pass_ratio"] == 0.0


# ---- parser tolerance ------------------------------------------------------


def test_parse_probe_log_tolerates_blank_and_noise_lines():
    lines = [
        "",
        "   ",
        "ambient line not a probe",
        "# comment",
        '[LATENCY_PROBE] {"engine":"deepgram","orgId":"o","roomId":"r","t0Ms":100,"audioChunkStartMs":100,"speechFinalMs":150,"segmentId":"a","broadcastSeq":1}',
        "{broken}",
    ]
    probes = parse_probe_log(lines)
    assert len(probes) == 1
    assert probes[0].broadcast_seq == 1
    assert probes[0].audio_chunk_start_abs_ms == 100
    assert probes[0].speech_final_abs_ms == 150


def test_parse_listener_jsonl_filters_non_ws_message_events():
    lines = [
        '{"event":"listener_capture_start","ts":"2026-10-09T14:52:35.587+00:00Z"}',
        '{"event":"ws_open","ts":"2026-10-09T14:52:59.342+00:00Z","room_id":"r"}',
        '{"event":"ws_message","format":"modern","seq":1,"meta_is_final":true,"meta_partial":false,"meta_segment_id":"a","ts":"2026-10-09T14:53:02.289+00:00Z"}',
        '{"event":"ws_heartbeat","state":"open","ts":"2026-10-09T14:53:07.300+00:00Z"}',
    ]
    records = parse_listener_jsonl(lines)
    assert len(records) == 1
    assert records[0].seq == 1


def test_join_prefers_broadcast_seq_over_segment_id():
    probes = parse_probe_log([
        '[LATENCY_PROBE] {"engine":"deepgram","orgId":"o","roomId":"r","t0Ms":1000,"audioChunkStartMs":1000,"speechFinalMs":1500,"segmentId":"X","broadcastSeq":7}'
    ])
    listeners = parse_listener_jsonl([
        '{\"event\":\"resolved\",\"roomId\":\"r\",\"ts\":\"1970-01-01T00:00:00.000+00:00Z\"}',
        '{"event":"ws_message","format":"modern","seq":7,"meta_is_final":true,"meta_partial":false,"meta_segment_id":"Z","ts":"1970-01-01T00:00:03.000+00:00Z"}'
    ])
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    full = [j for j in joined if j.orphan_reason is None]
    assert len(full) == 1
    assert full[0].key == ("r", "seq", 7)
    assert full[0].d1_end_to_end_ms == 1500
    assert full[0].d6_end_to_end_ms == 2000


def test_join_falls_back_to_segment_id_when_no_seq():
    probes = parse_probe_log([
        '[LATENCY_PROBE] {"engine":"deepgram","orgId":"o","roomId":"r","t0Ms":1000,"audioChunkStartMs":1000,"speechFinalMs":1500,"segmentId":"only-seg","broadcastSeq":null}'
    ])
    listeners = parse_listener_jsonl([
        '{\"event\":\"resolved\",\"roomId\":\"r\",\"ts\":\"1970-01-01T00:00:00.000+00:00Z\"}',
        '{"event":"ws_message","format":"modern","seq":null,"meta_is_final":true,"meta_partial":false,"meta_segment_id":"only-seg","ts":"1970-01-01T00:00:03.000+00:00Z"}'
    ])
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    full = [j for j in joined if j.orphan_reason is None]
    assert len(full) == 1
    assert full[0].key == ("r", "segment", "only-seg")


def test_modern_legacy_dedup_uses_first_arrival():
    probes = parse_probe_log([
        '[LATENCY_PROBE] {"engine":"deepgram","orgId":"o","roomId":"r","t0Ms":1000,"audioChunkStartMs":1000,"speechFinalMs":1500,"broadcastSeq":9}'
    ])
    listeners = parse_listener_jsonl([
        '{\"event\":\"resolved\",\"roomId\":\"r\",\"ts\":\"1970-01-01T00:00:00.000+00:00Z\"}',
        '{"event":"ws_message","format":"modern","seq":9,"meta_is_final":true,"meta_partial":false,"ts":"1970-01-01T00:00:02.000+00:00Z"}',
        '{"event":"ws_message","format":"legacy","seq":9,"meta_is_final":true,"meta_partial":false,"ts":"1970-01-01T00:00:02.300+00:00Z"}',
    ])
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    full = [j for j in joined if j.orphan_reason is None]
    assert len(full) == 1
    assert full[0].d1_end_to_end_ms == 500
    assert full[0].listener_first_recv_ms == 2000


def test_format_skew_warning_fires_above_100ms():
    probes = parse_probe_log([
        '[LATENCY_PROBE] {"engine":"deepgram","orgId":"o","roomId":"r","t0Ms":1000,"audioChunkStartMs":1000,"speechFinalMs":1500,"broadcastSeq":9}'
    ])
    listeners = parse_listener_jsonl([
        '{\"event\":\"resolved\",\"roomId\":\"r\",\"ts\":\"1970-01-01T00:00:00.000+00:00Z\"}',
        '{"event":"ws_message","format":"modern","seq":9,"meta_is_final":true,"meta_partial":false,"ts":"1970-01-01T00:00:02.000+00:00Z"}',
        '{"event":"ws_message","format":"legacy","seq":9,"meta_is_final":true,"meta_partial":false,"ts":"1970-01-01T00:00:02.300+00:00Z"}',
    ])
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    full = [j for j in joined if j.orphan_reason is None]
    assert full[0].format_skew_ms == 300
    assert full[0].format_skew_warning is True


def test_format_skew_warning_does_not_fire_within_100ms():
    probes = parse_probe_log([
        '[LATENCY_PROBE] {"engine":"deepgram","orgId":"o","roomId":"r","t0Ms":1000,"audioChunkStartMs":1000,"speechFinalMs":1500,"broadcastSeq":9}'
    ])
    listeners = parse_listener_jsonl([
        '{\"event\":\"resolved\",\"roomId\":\"r\",\"ts\":\"1970-01-01T00:00:00.000+00:00Z\"}',
        '{"event":"ws_message","format":"modern","seq":9,"meta_is_final":true,"meta_partial":false,"ts":"1970-01-01T00:00:02.000+00:00Z"}',
        '{"event":"ws_message","format":"legacy","seq":9,"meta_is_final":true,"meta_partial":false,"ts":"1970-01-01T00:00:02.050+00:00Z"}',
    ])
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    full = [j for j in joined if j.orphan_reason is None]
    assert full[0].format_skew_ms == 50
    assert full[0].format_skew_warning is False


# ---- measurement uncertainty policy -----------------------------------------
# Operator-mandated behavior: a result whose ±skew band straddles the
# threshold must classify as INCONCLUSIVE, not a silent PASS or FAIL.


def test_uncertainty_band_pass_fail_inconclusive_edges():
    # skew_ms = 1000, threshold = 10 (D1)
    assert uncertainty_band(5000, 10_000, 1000) == "PASS"
    assert uncertainty_band(8500, 10_000, 1000) == "PASS"    # 8500+1000=9500<10000
    # 9500+1000=10500 ≥ 10000 → inconclusive; 9500-1000=8500 ≤ 10000 → inconclusive
    assert uncertainty_band(9500, 10_000, 1000) == "INCONCLUSIVE"
    assert uncertainty_band(10_000, 10_000, 1000) == "INCONCLUSIVE"
    assert uncertainty_band(10_500, 10_000, 1000) == "INCONCLUSIVE"
    assert uncertainty_band(11_500, 10_000, 1000) == "FAIL"   # 11500-1000=10500>10000
    assert uncertainty_band(20_000, 10_000, 1000) == "FAIL"


def test_uncertainty_band_d6_boundaries():
    # D6 threshold = 15_000
    assert uncertainty_band(13_500, 15_000, 1000) == "PASS"   # 14500<15000
    assert uncertainty_band(14_500, 15_000, 1000) == "INCONCLUSIVE"
    assert uncertainty_band(15_500, 15_000, 1000) == "INCONCLUSIVE"
    assert uncertainty_band(16_500, 15_000, 1000) == "FAIL"   # 15500>15000


def test_uncertainty_band_zero_skew_is_definitive_but_edge_is_inconclusive():
    # With skew=0, inequalities are strict (per spec), so equal-to-threshold
    # falls through to INCONCLUSIVE. Operators who truly have a measured bound
    # may pass skew=0 for tight-tolerance analysis.
    assert uncertainty_band(9999, 10_000, 0) == "PASS"
    assert uncertainty_band(10_000, 10_000, 0) == "INCONCLUSIVE"
    assert uncertainty_band(10_001, 10_000, 0) == "FAIL"


def test_uncertainty_band_negative_skew_treated_as_absolute():
    # Defensive: a mis-passed negative skew behaves as its magnitude.
    assert uncertainty_band(10_500, 10_000, -1000) == "INCONCLUSIVE"


def test_default_clock_skew_is_none_v4():
    """v4: default is None (no measured bound). Operator must explicitly
    pass `--clock-skew-ms N` to assert a bound and derive PASS/FAIL."""
    assert DEFAULT_CLOCK_SKEW_MS is None


def test_join_classifies_boundary_result_as_inconclusive_under_default_skew():
    """Integration: a near-threshold measurement must land INCONCLUSIVE."""
    probes = parse_probe_log([
        '[LATENCY_PROBE] {"engine":"deepgram","orgId":"o","roomId":"r",'
        '"t0Ms":1000,"audioChunkStartMs":1000,"speechFinalMs":1500,'
        '"broadcastSeq":42}'
    ])
    # speechFinal=1500, recv=11000 → d1 = 9500 ms (within +/- 1000 of 10000)
    listeners = parse_listener_jsonl([
        '{\"event\":\"resolved\",\"roomId\":\"r\",\"ts\":\"1970-01-01T00:00:00.000+00:00Z\"}',
        '{"event":"ws_message","format":"modern","seq":42,'
        '"meta_is_final":true,"meta_partial":false,'
        '"ts":"1970-01-01T00:00:11.000+00:00Z"}'
    ])
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)  # default skew=1000
    full = [j for j in joined if j.orphan_reason is None]
    assert len(full) == 1
    assert full[0].d1_end_to_end_ms == 9500
    assert full[0].d1_verdict == "INCONCLUSIVE"


def test_summary_includes_inconclusive_counts():
    probes = parse_probe_log([
        '[LATENCY_PROBE] {"engine":"deepgram","orgId":"o","roomId":"r",'
        '"t0Ms":1000,"audioChunkStartMs":1000,"speechFinalMs":1500,'
        '"broadcastSeq":1}',
    ])
    listeners = parse_listener_jsonl([
        '{\"event\":\"resolved\",\"roomId\":\"r\",\"ts\":\"1970-01-01T00:00:00.000+00:00Z\"}',
        '{"event":"ws_message","format":"modern","seq":1,'
        '"meta_is_final":true,"meta_partial":false,'
        '"ts":"1970-01-01T00:00:11.000+00:00Z"}',
    ])
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    stats = summarize(joined)
    assert "d1_inconclusive_count" in stats
    assert "d6_inconclusive_count" in stats
    assert stats["d1_inconclusive_count"] == 1


# =====================================================================
# v4: cross-room scoping, impossible-negative-latency, no-clock-bound
# =====================================================================


def test_v4_cross_room_scoping_does_not_collide_on_seq():
    """Two rooms, SAME seq=5 in each. Join must key on (room, seq) and
    produce 2 joined entries, not 1 cross-room collision."""
    probes, listeners = load_from_paths(
        os.path.join(FIXTURE_DIR, "cross_room_probe.txt"),
        os.path.join(FIXTURE_DIR, "cross_room_listener.jsonl"),
    )
    assert len(probes) == 2
    joined = join_probe_and_listener(
        probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS
    )
    full = [j for j in joined if j.orphan_reason is None]
    assert len(full) == 2, f"expected 2 room-scoped joins, got {len(full)}: keys={[j.key for j in full]}"

    # Both must carry room-scoped 3-tuple keys.
    room_a = [j for j in full if j.key[0] == "room_A"]
    room_b = [j for j in full if j.key[0] == "room_B"]
    assert len(room_a) == 1 and len(room_b) == 1


def test_v4_cross_room_probe_without_listener_is_orphan_not_collision():
    """Probe for room A, listener recv in room B at same seq → both are
    orphans. Zero joins."""
    probes = parse_probe_log([
        '[LATENCY_PROBE] {"engine":"deepgram","orgId":"o","roomId":"room_A",'
        '"t0Ms":1000000000000,"audioChunkStartMs":1000000000000,'
        '"speechFinalMs":1000000001500,"broadcastSeq":5,"segmentId":"seg-A-5"}'
    ])
    listeners = parse_listener_jsonl([
        '{"event":"resolved","roomId":"room_B","ts":"2001-09-09T01:46:40.000+00:00Z"}',
        '{"event":"ws_message","format":"modern","seq":5,"meta_is_final":true,'
        '"meta_partial":false,"meta_segment_id":"seg-B-5","text_length":10,'
        '"ts":"2001-09-09T01:46:42.000+00:00Z"}',
    ])
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    full = [j for j in joined if j.orphan_reason is None]
    assert len(full) == 0, f"cross-room must not join, got {full}"
    orphan_classes = {j.orphan_reason for j in joined}
    assert "no_listener" in orphan_classes
    assert "listener_only" in orphan_classes


def test_v4_impossible_negative_latency_is_inconclusive_not_pass():
    """Listener recv BEFORE host emit by more than skew → verdict
    INCONCLUSIVE with impossible_negative_latency=True; NEVER PASS."""
    probes, listeners = load_from_paths(
        os.path.join(FIXTURE_DIR, "negative_latency_probe.txt"),
        os.path.join(FIXTURE_DIR, "negative_latency_listener.jsonl"),
    )
    joined = join_probe_and_listener(probes, listeners, clock_skew_ms=_V4_LEGACY_SKEW_MS)
    j = _by_key(joined, ("seq", 77))
    assert j.orphan_reason is None
    # Negative latencies observed:
    assert j.d1_end_to_end_ms is not None and j.d1_end_to_end_ms < -_V4_LEGACY_SKEW_MS
    assert j.d6_end_to_end_ms is not None and j.d6_end_to_end_ms < -_V4_LEGACY_SKEW_MS
    # Must NOT be PASS.
    assert j.d1_verdict == "INCONCLUSIVE"
    assert j.d6_verdict == "INCONCLUSIVE"
    assert j.impossible_negative_latency is True

    stats = summarize(joined)
    assert stats["n_impossible_negative_latency"] == 1


def test_v4_no_clock_bound_forces_inconclusive_on_everything():
    """Default clock_skew_ms=None → every joined utterance's D1 and D6
    verdicts are INCONCLUSIVE and flagged no_clock_bound_measured."""
    probes, listeners = load_from_paths(
        os.path.join(FIXTURE_DIR, "f2_probe_log.txt"),
        os.path.join(FIXTURE_DIR, "f2_listener.jsonl"),
    )
    # v4 default — DO NOT pass clock_skew_ms.
    joined = join_probe_and_listener(probes, listeners)
    full = [j for j in joined if j.orphan_reason is None]
    assert len(full) >= 1
    for j in full:
        # Even for latencies that would be PASS at skew=1000, verdict is
        # forced INCONCLUSIVE under no-bound.
        if j.d1_end_to_end_ms is not None:
            assert j.d1_verdict == "INCONCLUSIVE", f"{j.key} d1={j.d1_end_to_end_ms} verdict={j.d1_verdict}"
        if j.d6_end_to_end_ms is not None:
            assert j.d6_verdict == "INCONCLUSIVE", f"{j.key} d6={j.d6_end_to_end_ms} verdict={j.d6_verdict}"
        assert j.no_clock_bound_measured is True

    stats = summarize(joined)
    assert stats["n_no_clock_bound_measured"] >= 1
    assert stats["d1_pass_count"] == 0 and stats["d6_pass_count"] == 0


def test_v4_inherited_room_id_from_resolved_event():
    """ws_message rows without an explicit room_id must inherit from
    the preceding `resolved` event in the recorder context."""
    lines = [
        '{"event":"listener_capture_start","ts":"2026-10-09T14:52:35.587+00:00Z"}',
        '{"event":"resolved","orgId":"test2","roomId":"room_INHERITED",'
        '"ts":"2026-10-09T14:52:40.000+00:00Z"}',
        '{"event":"ws_open","room_id":"room_INHERITED","ts":"2026-10-09T14:52:40.100+00:00Z"}',
        # Note: no roomId on this ws_message row.
        '{"event":"ws_message","format":"modern","seq":42,"meta_is_final":true,'
        '"meta_partial":false,"meta_segment_id":"X","ts":"2026-10-09T14:52:45.000+00:00Z"}',
    ]
    records = parse_listener_jsonl(lines)
    assert len(records) == 1
    assert records[0].room_id == "room_INHERITED", (
        f"expected inherited room, got {records[0].room_id!r}"
    )


def test_v4_room_change_mid_stream_updates_inherited_context():
    """A second `resolved` event (e.g., reconnect across rooms) updates
    the inherited room_id for subsequent ws_message rows."""
    lines = [
        '{"event":"resolved","roomId":"room_FIRST","ts":"2026-10-09T14:52:40.000+00:00Z"}',
        '{"event":"ws_message","format":"modern","seq":1,"meta_is_final":true,'
        '"meta_partial":false,"ts":"2026-10-09T14:52:45.000+00:00Z"}',
        '{"event":"resolved","roomId":"room_SECOND","ts":"2026-10-09T14:53:00.000+00:00Z"}',
        '{"event":"ws_message","format":"modern","seq":1,"meta_is_final":true,'
        '"meta_partial":false,"ts":"2026-10-09T14:53:05.000+00:00Z"}',
    ]
    records = parse_listener_jsonl(lines)
    assert len(records) == 2
    assert records[0].room_id == "room_FIRST"
    assert records[1].room_id == "room_SECOND"


def test_v4_boundary_tests_under_1s_skew():
    """7 boundary points covering D1 threshold 10 000 and D6 threshold 15 000
    under explicit skew=1000. Policy: INCONCLUSIVE when band straddles."""
    cases = [
        # D1 at 8_500 → PASS (8500+1000 < 10000)
        (8_500, 10_000, 1_000, "PASS"),
        # D1 at 9_500 → INCONCLUSIVE (9500+1000 = 10500 > 10000 AND 9500-1000 = 8500 < 10000)
        (9_500, 10_000, 1_000, "INCONCLUSIVE"),
        (10_000, 10_000, 1_000, "INCONCLUSIVE"),
        (10_500, 10_000, 1_000, "INCONCLUSIVE"),
        (11_500, 10_000, 1_000, "FAIL"),
        # D6 at 13_500 → PASS, 14_500/15_000/15_500 → INCONCLUSIVE, 16_500 → FAIL
        (13_500, 15_000, 1_000, "PASS"),
        (14_500, 15_000, 1_000, "INCONCLUSIVE"),
        (15_500, 15_000, 1_000, "INCONCLUSIVE"),
        (16_500, 15_000, 1_000, "FAIL"),
    ]
    for result, threshold, skew, expected in cases:
        got = uncertainty_band(result, threshold, skew)
        assert got == expected, f"result={result} threshold={threshold} skew={skew}: expected {expected}, got {got}"


def test_v4_boundary_with_zero_skew_collapses_inconclusive_to_pass_or_fail():
    """With explicit skew=0 (operator asserts zero skew), all previously
    INCONCLUSIVE verdicts collapse to definitive PASS/FAIL."""
    assert uncertainty_band(9_500, 10_000, 0) == "PASS"
    assert uncertainty_band(10_000, 10_000, 0) == "INCONCLUSIVE"  # at the line
    assert uncertainty_band(10_500, 10_000, 0) == "FAIL"

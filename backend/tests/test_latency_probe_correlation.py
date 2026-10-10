"""F2 follow-up v2: LatencyProbe correlation fields + D1/D6 split + back-compat.

The emit path requires LATENCY_PROBE_ENABLED to be truthy — force it on
inside each test via monkeypatch so a global `LATENCY_PROBE_ENABLED=0`
in CI doesn't silence the probe.
"""
from __future__ import annotations

import io
import json
import re
from contextlib import redirect_stdout

import pytest

from app import latency_probe as _lp
from app.latency_probe import LatencyProbe


@pytest.fixture(autouse=True)
def _force_probe_enabled(monkeypatch):
    monkeypatch.setattr(_lp, "ENABLED", True)
    yield


_PROBE_RE = re.compile(r"\[LATENCY_PROBE\]\s+(\{.*\})")


def _emit_capture(probe: LatencyProbe) -> dict | None:
    buf = io.StringIO()
    with redirect_stdout(buf):
        probe.emit_and_reset()
    text = buf.getvalue().strip()
    if not text:
        return None
    m = _PROBE_RE.search(text)
    assert m, f"expected probe prefix in: {text!r}"
    return json.loads(m.group(1))


def test_probe_emits_new_correlation_fields():
    p = LatencyProbe("deepgram", "test2", "room_abc")
    p.mark_t0()
    p.mark_speech_final(segment_id="seg-1")
    p.mark_t1()
    p.mark_t2()
    p.mark_t3()
    p.record_broadcast(seq=42)
    payload = _emit_capture(p)
    assert payload is not None
    assert payload["engine"] == "deepgram"
    assert payload["orgId"] == "test2"
    assert payload["roomId"] == "room_abc"
    assert payload["broadcastSeq"] == 42
    assert payload["segmentId"] == "seg-1"
    assert isinstance(payload["audioChunkStartMs"], int)
    assert payload["audioChunkStartMs"] == payload["t0Ms"]
    assert isinstance(payload["speechFinalMs"], int)
    assert payload["speechFinalMs"] >= payload["audioChunkStartMs"]
    assert isinstance(payload["speechFinalDeltaMs"], int)
    assert payload["speechFinalDeltaMs"] >= 0
    assert payload["utteranceEndMs"] == payload["speechFinalDeltaMs"]
    assert payload["utteranceEndAbsMs"] == payload["speechFinalMs"]


def test_mark_utterance_end_is_alias_for_mark_speech_final():
    p = LatencyProbe("deepgram", "test2", "room_abc")
    p.mark_t0()
    p.mark_utterance_end(segment_id="seg-legacy")
    payload = _emit_capture(p)
    assert payload is not None
    assert payload["segmentId"] == "seg-legacy"
    assert payload["speechFinalMs"] is not None


def test_probe_reset_clears_all_slots():
    p = LatencyProbe("deepgram", "test2", "room_abc")
    p.mark_t0()
    p.mark_speech_final(segment_id="seg-1")
    p.record_broadcast(seq=42)
    _emit_capture(p)

    p.mark_t0()
    p.mark_t2()
    payload = _emit_capture(p)
    assert payload is not None
    assert payload["segmentId"] is None
    assert payload["broadcastSeq"] is None
    assert payload["speechFinalMs"] is None
    assert payload["speechFinalDeltaMs"] is None
    assert payload["audioChunkStartMs"] is not None
    assert payload["audioChunkStartMs"] == payload["t0Ms"]


def test_probe_backward_compat_old_mark_sequence():
    p = LatencyProbe("deepgram", "test2", "room_abc")
    p.mark_t0()
    p.mark_t1()
    p.mark_t2()
    p.mark_t3()
    payload = _emit_capture(p)
    assert payload is not None
    assert payload["dT1Ms"] is not None
    assert payload["dT2Ms"] is not None
    assert payload["dT3Ms"] is not None
    assert payload["speechFinalMs"] is None
    assert payload["speechFinalDeltaMs"] is None
    assert payload["utteranceEndMs"] is None
    assert payload["utteranceEndAbsMs"] is None
    assert payload["audioChunkStartMs"] is not None
    assert payload["segmentId"] is None
    assert payload["broadcastSeq"] is None


def test_record_broadcast_before_mark_speech_final_also_captures_segment_id():
    p = LatencyProbe("deepgram", "test2", "room_abc")
    p.mark_t0()
    p.record_broadcast(seq=10, segment_id="seg-from-broadcast")
    p.mark_speech_final()
    payload = _emit_capture(p)
    assert payload is not None
    assert payload["segmentId"] == "seg-from-broadcast"
    assert payload["broadcastSeq"] == 10


def test_mark_speech_final_guard_without_t0():
    p = LatencyProbe("deepgram", "test2", "room_abc")
    p.mark_speech_final(segment_id="seg-x")
    assert p.speech_final_ms is None
    assert p.utterance_end_ms is None
    assert p.segment_id is None


def test_audio_chunk_start_equals_t0_on_first_mark():
    p = LatencyProbe("deepgram", "test2", "room_abc")
    p.mark_t0()
    assert p.t0_ms is not None
    assert p.audio_chunk_start_ms == p.t0_ms

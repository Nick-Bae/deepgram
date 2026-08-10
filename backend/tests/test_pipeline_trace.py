from __future__ import annotations

import json
import unittest
from io import StringIO
from unittest.mock import patch

from app.pipeline_trace import PipelineTrace


class PipelineTraceTests(unittest.TestCase):
    def test_defaults_are_none_except_utterance_id(self) -> None:
        trace = PipelineTrace()
        self.assertIsNotNone(trace.utterance_id)
        self.assertEqual(len(trace.utterance_id), 32)  # uuid4().hex
        self.assertIsNone(trace.audio_first_partial_at)
        self.assertIsNone(trace.committed_at)

    def test_mark_audio_first_partial_only_records_first_call(self) -> None:
        trace = PipelineTrace()
        trace.mark_audio_first_partial()
        first = trace.audio_first_partial_at
        self.assertIsNotNone(first)
        trace.mark_audio_first_partial()
        self.assertEqual(trace.audio_first_partial_at, first)

    def test_mark_candidate_only_records_first_qualifying(self) -> None:
        trace = PipelineTrace()
        trace.mark_candidate("seg-42", 0.91)
        self.assertEqual(trace.candidate_seg_id, "seg-42")
        self.assertEqual(trace.candidate_score, 0.91)
        trace.mark_candidate("seg-43", 0.88)  # reset should not overwrite trace
        self.assertEqual(trace.candidate_seg_id, "seg-42")

    def test_mark_preview_overwrites_on_corrective_replacement(self) -> None:
        trace = PipelineTrace()
        trace.mark_preview("seg-42", 0.91, prefix_len=12)
        first_entered = trace.preview_entered_at
        trace.mark_preview("seg-45", 0.93, prefix_len=15)
        self.assertEqual(trace.preview_seg_id, "seg-45")
        self.assertGreaterEqual(trace.preview_entered_at or 0, first_entered or 0)

    def test_mark_corrective_replacement_records_from_and_to(self) -> None:
        trace = PipelineTrace()
        trace.mark_corrective_replacement("seg-42", "seg-45")
        self.assertEqual(trace.corrective_from_seg_id, "seg-42")
        self.assertEqual(trace.corrective_to_seg_id, "seg-45")
        self.assertIsNotNone(trace.corrective_replacement_at)

    def test_mark_committed_sets_source_and_score(self) -> None:
        trace = PipelineTrace()
        trace.mark_committed(whole_sentence_score=0.88, source="reviewed")
        self.assertEqual(trace.committed_source, "reviewed")
        self.assertEqual(trace.committed_whole_sentence_score, 0.88)
        self.assertIsNotNone(trace.committed_at)

    def test_apply_client_echo_ignores_missing_fields(self) -> None:
        trace = PipelineTrace()
        trace.apply_client_echo(received_at=1000, rendered_at=None)
        self.assertEqual(trace.client_received_at, 1000)
        self.assertIsNone(trace.client_rendered_at)

    def test_to_broadcast_payload_has_only_utterance_id_and_broadcast_at(self) -> None:
        trace = PipelineTrace(org_id="org-1", room_id="room-1")
        trace.mark_broadcast_sent()
        payload = trace.to_broadcast_payload()
        self.assertEqual(set(payload.keys()), {"utteranceId", "broadcastSentAt"})
        self.assertEqual(payload["utteranceId"], trace.utterance_id)
        self.assertIsNotNone(payload["broadcastSentAt"])

    def test_to_log_dict_uses_camel_case(self) -> None:
        trace = PipelineTrace(org_id="org-1", room_id="room-1")
        trace.mark_committed(whole_sentence_score=0.9, source="reviewed")
        d = trace.to_log_dict()
        self.assertIn("committedAt", d)
        self.assertIn("committedWholeSentenceScore", d)
        self.assertIn("committedSource", d)
        self.assertIn("orgId", d)
        self.assertIn("roomId", d)
        self.assertNotIn("committed_at", d)
        self.assertNotIn("org_id", d)

    def test_emit_writes_pipeline_trace_prefixed_json_line(self) -> None:
        trace = PipelineTrace(org_id="org-1", room_id="room-1")
        trace.mark_committed(whole_sentence_score=0.88, source="reviewed")
        buf = StringIO()
        with patch("builtins.print") as p:
            trace.emit()
            self.assertTrue(p.called)
            (line,), _ = p.call_args
            self.assertTrue(line.startswith("[PIPELINE_TRACE] "))
            payload = json.loads(line[len("[PIPELINE_TRACE] "):])
            self.assertEqual(payload["orgId"], "org-1")
            self.assertEqual(payload["committedSource"], "reviewed")


if __name__ == "__main__":
    unittest.main()

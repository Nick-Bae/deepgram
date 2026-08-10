from __future__ import annotations

import asyncio
import json
import unittest
from unittest.mock import patch

from starlette.requests import Request

from app.routes.pipeline_trace import receive_client_trace


def _fake_request(body: bytes) -> Request:
    """Build a minimal Starlette Request wired to the given raw body."""
    scope = {
        "type": "http",
        "method": "POST",
        "headers": [(b"content-type", b"application/json")],
        "path": "/api/pipeline_trace",
    }
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}
    return Request(scope, receive)


class PipelineTraceRouteTests(unittest.TestCase):
    def _run(self, coro):
        return asyncio.new_event_loop().run_until_complete(coro)

    def test_happy_path_logs_and_returns_ok(self) -> None:
        payload = json.dumps({
            "utteranceId": "abc123",
            "receivedAt": 1_700_000_000_000,
            "renderedAt": 1_700_000_000_100,
        }).encode()
        with patch("builtins.print") as p:
            resp = self._run(receive_client_trace(_fake_request(payload)))
        self.assertEqual(resp, {"ok": True})
        self.assertTrue(p.called)
        (line,), _ = p.call_args
        self.assertTrue(line.startswith("[PIPELINE_TRACE_CLIENT] "))
        parsed = json.loads(line[len("[PIPELINE_TRACE_CLIENT] "):])
        self.assertEqual(parsed["utteranceId"], "abc123")
        self.assertEqual(parsed["receivedAt"], 1_700_000_000_000)
        self.assertEqual(parsed["renderedAt"], 1_700_000_000_100)

    def test_missing_utterance_id_returns_error(self) -> None:
        payload = json.dumps({"receivedAt": 1000}).encode()
        resp = self._run(receive_client_trace(_fake_request(payload)))
        self.assertEqual(resp, {"ok": False, "reason": "missing_utterance_id"})

    def test_invalid_json_returns_error(self) -> None:
        resp = self._run(receive_client_trace(_fake_request(b"not-json")))
        self.assertEqual(resp["ok"], False)
        self.assertEqual(resp["reason"], "invalid_json")

    def test_non_dict_payload_returns_error(self) -> None:
        resp = self._run(receive_client_trace(_fake_request(b'"a string"')))
        self.assertEqual(resp, {"ok": False, "reason": "invalid_payload"})

    def test_coerces_string_timestamps_or_drops_them(self) -> None:
        payload = json.dumps({
            "utteranceId": "x",
            "receivedAt": "1700000000000",
            "renderedAt": "not-a-number",
        }).encode()
        with patch("builtins.print"):
            self._run(receive_client_trace(_fake_request(payload)))


if __name__ == "__main__":
    unittest.main()

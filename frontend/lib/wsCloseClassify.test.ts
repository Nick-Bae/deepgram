// Unit tests for the shared close-classifier. The reviewer's spec:
//   - room_ended reason OR code 4001 → terminal (stop; do not reconnect).
//   - Any other code (1000-clean without room_ended, 1001, 1006, 1012 SIGTERM,
//     4000-4999 app-specific except 4001) → transient (reconnect).
//
// The class of bug this suite pins: an earlier host-producer implementation
// treated every non-error close as "reconnect", which meant a room_ended
// close spun a reconnect loop instead of releasing the microphone. The
// listener side (useSubtitleSocket) already classified correctly, but
// Uvicorn's 1012 (Service Restart) close on SIGTERM must NOT tombstone
// the listener page — those tests are also here so a future refactor
// can't regress that path.

import test from "node:test";
import assert from "node:assert/strict";
import {
  classifyWebSocketClose,
  isTerminalRoomClose,
} from "./wsCloseClassify.ts";

test("terminal: code 4001, any reason", () => {
  assert.equal(classifyWebSocketClose({ code: 4001, reason: "" }), "terminal_room_ended");
  assert.equal(classifyWebSocketClose({ code: 4001, reason: "whatever" }), "terminal_room_ended");
  assert.equal(isTerminalRoomClose({ code: 4001, reason: "" }), true);
});

test("terminal: code 1000 with reason=room_ended (the canonical close)", () => {
  assert.equal(classifyWebSocketClose({ code: 1000, reason: "room_ended" }), "terminal_room_ended");
  assert.equal(isTerminalRoomClose({ code: 1000, reason: "room_ended" }), true);
});

test("terminal: reason=room_ended is recognized even without code 1000", () => {
  // Defensive: if the backend ever sends reason=room_ended with a different
  // code (unlikely, but robust to backend evolution), we still classify
  // as terminal. Matches the listener's historical behavior.
  assert.equal(
    classifyWebSocketClose({ code: 1005, reason: "room_ended" }),
    "terminal_room_ended",
  );
});

test("transient: code 1012 (Uvicorn SIGTERM Service Restart) — MUST NOT tombstone", () => {
  // Reviewer's F-9 requirement: on Cloud Run instance restart, Uvicorn
  // 0.34 closes active WebSockets with 1012. Listeners must reconnect
  // (potentially to another instance); hosts must reconnect AND keep
  // the microphone pipeline they already acquired.
  assert.equal(classifyWebSocketClose({ code: 1012, reason: "" }), "transient");
  assert.equal(isTerminalRoomClose({ code: 1012, reason: "" }), false);
});

test("transient: code 1001 (Going Away) — browser tab background, DNS change, etc.", () => {
  assert.equal(classifyWebSocketClose({ code: 1001, reason: "" }), "transient");
});

test("transient: code 1006 (Abnormal Closure) — network drops, no clean handshake", () => {
  assert.equal(classifyWebSocketClose({ code: 1006, reason: "" }), "transient");
});

test("transient: code 1000 without reason=room_ended (generic clean close)", () => {
  // A bare code 1000 close (e.g. from a load balancer) is NOT the
  // canonical room-end signal. Must reconnect.
  assert.equal(classifyWebSocketClose({ code: 1000, reason: "" }), "transient");
});

test("transient: unknown app-specific codes other than 4001", () => {
  assert.equal(classifyWebSocketClose({ code: 4000, reason: "" }), "transient");
  assert.equal(classifyWebSocketClose({ code: 4002, reason: "" }), "transient");
  assert.equal(classifyWebSocketClose({ code: 4999, reason: "" }), "transient");
});

test("transient: reason field absent (undefined) — treated as empty", () => {
  assert.equal(classifyWebSocketClose({ code: 1012 }), "transient");
  assert.equal(classifyWebSocketClose({ code: 1000 }), "transient");
});

test("regression: the previous producer bug — 1000+room_ended must not fall through to reconnect", () => {
  // Historic bug: useDeepgramProducer's onclose scheduled a reconnect
  // for every close that wasn't a terminalErrorRef event. That meant
  // a host whose room ended would immediately reconnect, the backend
  // would immediately re-close with room_ended, and the loop repeated.
  // isTerminalRoomClose returning true here forces the terminal branch.
  const closeEvent = { code: 1000, reason: "room_ended" };
  assert.equal(isTerminalRoomClose(closeEvent), true, "room_ended must be terminal");
});

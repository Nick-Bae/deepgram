// Regression tests for the viewer-page display precedence — the bug where
// a viewer with existing translated lines kept showing the last sentence
// after End Service instead of "Broadcast ended.".
//
// Runs with Node's built-in test runner (Node >= 22), no framework needed:
//
//   node --test --experimental-strip-types utils/viewerDisplay.test.ts
//
// The suite covers only the pure display-precedence logic. WebSocket
// close-reason handling and "no replacement WebSocket" behavior are covered
// separately in useSubtitleSocket + the backend test_room_cleanup.py suite.

import test from "node:test";
import assert from "node:assert/strict";
import {
  endReasonForEndedRoom,
  resolveViewerDisplay,
  roomEndMessage,
  socketTerminatedForCurrentRoom,
  subtitleModeLines,
} from "./viewerDisplay.ts";

test("terminal state replaces existing translation lines with 'Broadcast ended.'", () => {
  const { currentEn, recentEn, isTerminal } = resolveViewerDisplay({
    serviceEnded: true,
    socketTerminated: false,
    displayEnLines: [
      "Welcome to the service.",
      "The Lord is my shepherd.",
      "I shall not want.",
    ],
    waitingMessage: "Live — waiting for speech…",
    lastEndReason: "host_end",
  });
  assert.equal(isTerminal, true);
  assert.equal(currentEn, "Broadcast ended.");
  assert.deepEqual(recentEn, []);
});

test("terminal state without any translation lines still shows 'Broadcast ended.'", () => {
  const { currentEn, recentEn, isTerminal } = resolveViewerDisplay({
    serviceEnded: true,
    socketTerminated: false,
    displayEnLines: [],
    waitingMessage: "Connecting…",
    lastEndReason: "host_end",
  });
  assert.equal(isTerminal, true);
  assert.equal(currentEn, "Broadcast ended.");
  assert.deepEqual(recentEn, []);
});

test("socketTerminated alone (before serviceEnded flips) also displays 'Broadcast ended.'", () => {
  // useSubtitleSocket fires terminated=true when a close-with-reason=room_ended
  // arrives. That happens BEFORE the next /resolve poll flips serviceEnded.
  // The listener must react immediately on either flag.
  const { currentEn, recentEn, isTerminal } = resolveViewerDisplay({
    serviceEnded: false,
    socketTerminated: true,
    displayEnLines: [
      "Sermon in progress.",
      "The last thing they heard.",
    ],
    waitingMessage: "Live — waiting for speech…",
    lastEndReason: null,
  });
  assert.equal(isTerminal, true);
  assert.equal(currentEn, "Broadcast ended.");
  assert.deepEqual(recentEn, []);
});

test("non-terminal + populated lines: currentEn is the last line, recentEn is up to 2 before it", () => {
  const { currentEn, recentEn, isTerminal } = resolveViewerDisplay({
    serviceEnded: false,
    socketTerminated: false,
    displayEnLines: ["a", "b", "c", "d"],
    waitingMessage: "Live — waiting for speech…",
    lastEndReason: null,
  });
  assert.equal(isTerminal, false);
  assert.equal(currentEn, "d");
  assert.deepEqual(recentEn, ["b", "c"]);
});

test("non-terminal + empty lines: currentEn falls back to waitingMessage", () => {
  const { currentEn, recentEn, isTerminal } = resolveViewerDisplay({
    serviceEnded: false,
    socketTerminated: false,
    displayEnLines: [],
    waitingMessage: "Live — waiting for speech…",
    lastEndReason: null,
  });
  assert.equal(isTerminal, false);
  assert.equal(currentEn, "Live — waiting for speech…");
  assert.deepEqual(recentEn, []);
});

test("terminal message uses the specific end-reason string when one is set", () => {
  const { currentEn } = resolveViewerDisplay({
    serviceEnded: true,
    socketTerminated: false,
    displayEnLines: ["stale"],
    waitingMessage: "unused",
    lastEndReason: "trial_expired",
  });
  assert.equal(currentEn, "Broadcast stopped: trial minutes exhausted.");
});

test("roomEndMessage returns null for an unknown reason", () => {
  assert.equal(roomEndMessage("something_unexpected"), null);
  assert.equal(roomEndMessage(null), null);
  assert.equal(roomEndMessage(undefined), null);
  assert.equal(roomEndMessage(""), null);
});

// Subtitle-mode collapse. The listener page's default (subtitle) view
// iterates a lines array and styles the last one as "current". Without
// this collapse, terminal state would render currentEn correctly only in
// fullscreen mode while subtitle mode kept showing the lingering last
// translation (the exact regression reported after PR #17).

test("subtitleModeLines collapses to [currentEn] when terminal", () => {
  const lines = subtitleModeLines(true, "Broadcast ended.", [
    "welcome",
    "the last sentence",
  ]);
  assert.deepEqual(lines, ["Broadcast ended."]);
});

test("subtitleModeLines passes through displayEnLines when not terminal", () => {
  const lines = subtitleModeLines(false, "unused", ["one", "two", "three"]);
  assert.deepEqual(lines, ["one", "two", "three"]);
});

test("subtitleModeLines keeps a single-element terminal array even if displayEnLines is empty", () => {
  const lines = subtitleModeLines(true, "Broadcast ended.", []);
  assert.deepEqual(lines, ["Broadcast ended."]);
});

// ── socketTerminatedForCurrentRoom ─────────────────────────────────────
// Room-scoping the raw socketTerminated flag. Without this, a stale
// terminal event for room A would paint "Broadcast ended." over a live
// room B the instant it fires — the tombstone path clears one render
// later, so the display would briefly flip terminal before flipping back.

test("socketTerminatedForCurrentRoom: fires when terminated room matches the current activeRoomId", () => {
  assert.equal(
    socketTerminatedForCurrentRoom({
      socketTerminated: true,
      socketTerminatedRoomId: "room-A",
      activeRoomId: "room-A",
      lastRoomId: null,
    }),
    true,
  );
});

test("socketTerminatedForCurrentRoom: fires when activeRoomId is null and terminated room matches lastRoomId", () => {
  assert.equal(
    socketTerminatedForCurrentRoom({
      socketTerminated: true,
      socketTerminatedRoomId: "room-A",
      activeRoomId: null,
      lastRoomId: "room-A",
    }),
    true,
  );
});

test("socketTerminatedForCurrentRoom: does NOT fire when a new live room has taken over", () => {
  assert.equal(
    socketTerminatedForCurrentRoom({
      socketTerminated: true,
      socketTerminatedRoomId: "room-A",
      activeRoomId: "room-B",
      lastRoomId: "room-A",
    }),
    false,
  );
});

test("socketTerminatedForCurrentRoom: does NOT fire without a terminated room ID", () => {
  assert.equal(
    socketTerminatedForCurrentRoom({
      socketTerminated: true,
      socketTerminatedRoomId: null,
      activeRoomId: "room-A",
      lastRoomId: null,
    }),
    false,
  );
});

test("socketTerminatedForCurrentRoom: does NOT fire when socketTerminated is false", () => {
  assert.equal(
    socketTerminatedForCurrentRoom({
      socketTerminated: false,
      socketTerminatedRoomId: "room-A",
      activeRoomId: "room-A",
      lastRoomId: null,
    }),
    false,
  );
});

// ── endReasonForEndedRoom ──────────────────────────────────────────────
// The /resolve `lastEndReason` refers to the most recent ended room in
// backend view. Applying it regardless would let an unrelated ended
// room's specific message leak onto a different tombstoned room.

test("endReasonForEndedRoom: reason applies when lastRoomId matches the tombstone", () => {
  assert.equal(
    endReasonForEndedRoom({
      endedRoomId: "room-A",
      activeRoomId: null,
      roomStatus: "",
      lastRoomId: "room-A",
      lastRoomStatus: "ended",
      lastEndReason: "trial_expired",
    }),
    "trial_expired",
  );
});

test("endReasonForEndedRoom: reason applies when active room matches the tombstone AND status is ended", () => {
  assert.equal(
    endReasonForEndedRoom({
      endedRoomId: "room-A",
      activeRoomId: "room-A",
      roomStatus: "ended",
      lastRoomId: null,
      lastRoomStatus: "",
      lastEndReason: "host_end",
    }),
    "host_end",
  );
});

test("endReasonForEndedRoom: reason does NOT apply when lastRoomId is a different (unrelated) room", () => {
  assert.equal(
    endReasonForEndedRoom({
      endedRoomId: "room-A",
      activeRoomId: null,
      roomStatus: "",
      lastRoomId: "room-Z",
      lastRoomStatus: "ended",
      lastEndReason: "monthly_limit_reached",
    }),
    null,
  );
});

test("endReasonForEndedRoom: reason does NOT apply when there is no tombstone", () => {
  assert.equal(
    endReasonForEndedRoom({
      endedRoomId: null,
      activeRoomId: "room-A",
      roomStatus: "live",
      lastRoomId: "room-A",
      lastRoomStatus: "ended",
      lastEndReason: "host_end",
    }),
    null,
  );
});

test("endReasonForEndedRoom: falls back to null (→ generic 'Broadcast ended.') when the reason is not room-scoped to the tombstone", () => {
  // The socket terminated for room-A before /resolve caught up; /resolve
  // still shows an unrelated older last room. Reason must NOT apply.
  assert.equal(
    endReasonForEndedRoom({
      endedRoomId: "room-A",
      activeRoomId: "room-A",
      roomStatus: "live",  // /resolve hasn't observed the end yet
      lastRoomId: "room-Z",
      lastRoomStatus: "ended",
      lastEndReason: "idle_timeout",
    }),
    null,
  );
});

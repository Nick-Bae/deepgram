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
import { resolveViewerDisplay, roomEndMessage } from "./viewerDisplay.ts";

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

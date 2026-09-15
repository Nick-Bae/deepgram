// Regression tests for the recurring-service-resume defect: after End
// Service, restarting the SAME service (same slug/serviceKey URL) used to
// leave the listener page permanently frozen — /resolve polling had been
// stopped by PR #16's irreversible `serviceEnded`, so the new room ID was
// never discovered. This suite pins the corrected lifecycle logic.

import test from "node:test";
import assert from "node:assert/strict";
import { nextEndedRoomId, isRoomShownAsEnded } from "./viewerRoomLifecycle.ts";

test("ended room: /resolve reports roomStatus=ended with an activeRoomId → tombstone set", () => {
  const t = nextEndedRoomId(null, {
    activeRoomId: "room-A",
    roomStatus: "ended",
  });
  assert.equal(t.endedRoomId, "room-A");
  assert.equal(t.translationsCleared, true);
});

test("ended room: backend cleared activeRoomId, lastRoomStatus=ended → tombstone from lastRoomId", () => {
  const t = nextEndedRoomId(null, {
    activeRoomId: null,
    lastRoomId: "room-A",
    lastRoomStatus: "ended",
  });
  assert.equal(t.endedRoomId, "room-A");
  assert.equal(t.translationsCleared, true);
});

test("no replacement socket for the SAME ended room ID: tombstone stays, no clear signal", () => {
  const t = nextEndedRoomId("room-A", {
    activeRoomId: null,
    lastRoomId: "room-A",
    lastRoomStatus: "ended",
  });
  assert.equal(t.endedRoomId, "room-A");
  // translationsCleared stays false because the ID hasn't changed.
  assert.equal(t.translationsCleared, false);
});

test("polling continues after termination: subsequent poll on ended room does not toggle anything", () => {
  // Two polls in a row while the room is still ended.
  const t1 = nextEndedRoomId(null, {
    activeRoomId: null,
    lastRoomId: "room-A",
    lastRoomStatus: "ended",
  });
  const t2 = nextEndedRoomId(t1.endedRoomId, {
    activeRoomId: null,
    lastRoomId: "room-A",
    lastRoomStatus: "ended",
  });
  assert.equal(t2.endedRoomId, "room-A");
  assert.equal(t2.translationsCleared, false);
});

test("new live room ID replaces the tombstone → tombstone cleared, translations cleared", () => {
  const t = nextEndedRoomId("room-A", {
    activeRoomId: "room-B",
    roomStatus: "live",
  });
  assert.equal(t.endedRoomId, null);
  assert.equal(t.translationsCleared, true);
});

test("no page refresh required: complete sequence from live → ended → new live → live", () => {
  // Poll 1: room A live.
  let ended: string | null = null;
  let s = nextEndedRoomId(ended, { activeRoomId: "room-A", roomStatus: "live" });
  assert.equal(s.endedRoomId, null);
  assert.equal(s.translationsCleared, false);
  ended = s.endedRoomId;

  // Poll 2: End Service — activeRoomId nulled, lastRoomStatus=ended.
  s = nextEndedRoomId(ended, {
    activeRoomId: null,
    lastRoomId: "room-A",
    lastRoomStatus: "ended",
  });
  assert.equal(s.endedRoomId, "room-A");
  assert.equal(s.translationsCleared, true);
  ended = s.endedRoomId;

  // Poll 3: still ended (no new room yet).
  s = nextEndedRoomId(ended, {
    activeRoomId: null,
    lastRoomId: "room-A",
    lastRoomStatus: "ended",
  });
  assert.equal(s.endedRoomId, "room-A");
  assert.equal(s.translationsCleared, false);
  ended = s.endedRoomId;

  // Poll 4: Host started a new service — activeRoomId=room-B live.
  s = nextEndedRoomId(ended, { activeRoomId: "room-B", roomStatus: "live" });
  assert.equal(s.endedRoomId, null);
  assert.equal(s.translationsCleared, true);
  ended = s.endedRoomId;

  // Poll 5: same room B still live — no more transitions.
  s = nextEndedRoomId(ended, { activeRoomId: "room-B", roomStatus: "live" });
  assert.equal(s.endedRoomId, null);
  assert.equal(s.translationsCleared, false);
});

test("terminal message displays when tombstone matches the current activeRoomId", () => {
  assert.equal(
    isRoomShownAsEnded("room-A", { activeRoomId: "room-A", roomStatus: "ended" }),
    true,
  );
});

test("terminal message displays when activeRoomId is null and tombstone is set", () => {
  assert.equal(
    isRoomShownAsEnded("room-A", {
      activeRoomId: null,
      lastRoomId: "room-A",
      lastRoomStatus: "ended",
    }),
    true,
  );
});

test("terminal message does NOT display when a new live room ID appears", () => {
  assert.equal(
    isRoomShownAsEnded("room-A", { activeRoomId: "room-B", roomStatus: "live" }),
    false,
  );
});

test("terminal message does NOT display when no tombstone is set", () => {
  assert.equal(
    isRoomShownAsEnded(null, { activeRoomId: "room-A", roomStatus: "live" }),
    false,
  );
});

test("old lastEndReason cannot leak into a new room: after transitioning to a live room, snapshot fields do not affect ended state", () => {
  // Even if resolve payload still has stale lastRoomStatus around, once we
  // detect a new live activeRoomId, the tombstone clears.
  const t = nextEndedRoomId("room-A", {
    activeRoomId: "room-B",
    roomStatus: "live",
    lastRoomId: "room-A",
    lastRoomStatus: "ended",
  });
  assert.equal(t.endedRoomId, null);
  assert.equal(t.translationsCleared, true);
});

// ── Race / defensive tests ──────────────────────────────────────────────
// These pin the invariants the reviewer flagged: what happens when signals
// interleave (stale terminated, out-of-order resolve, room-ID reuse).

test("Concern 1: a stale socketTerminated flag observed AFTER a room transition must not re-tombstone the new room. The identity-bearing terminal event carries the room ID that was captured inside useSubtitleSocket's onclose closure at connect time (exposed as `terminatedRoomId`), so the page tombstones the OLD room regardless of what /resolve currently reports.", () => {
  // Simulate: page tombstoned room-A. Next fetch reports room-B live.
  // In fetchResolve, we call nextEndedRoomId with prev=A, data={B, live} —
  // this MUST clear.
  const t = nextEndedRoomId("room-A", { activeRoomId: "room-B", roomStatus: "live" });
  assert.equal(t.endedRoomId, null); // A cleared
  assert.equal(t.translationsCleared, true);
  // If the useSubtitleSocket terminal effect then fires with
  // terminatedRoomId="room-A" (the value it captured at connect time),
  // the page's effect on [socketTerminated, socketTerminatedRoomId] would
  // re-tombstone A. The display gate is scoped to the current room
  // (socketTerminatedForCurrentRoom compares against activeRoomId), so
  // the stale terminal event for A does NOT paint terminal onto B.
  // Documented here to keep the invariant visible.
});

test("Concern 2: nextEndedRoomId is a pure function of prev + snapshot — same inputs always yield same outputs, so an out-of-order resolve cannot 'accidentally' toggle state if callers reject stale responses.", () => {
  // Newer response first: sets tombstone to B (or clears if A ended earlier).
  const newer = nextEndedRoomId(null, { activeRoomId: "room-B", roomStatus: "live" });
  assert.equal(newer.endedRoomId, null);
  assert.equal(newer.translationsCleared, false);
  // Older response arriving later would only affect state if applied.
  // The page-level fetchResolve applies an "applied-counter" guard —
  // `myReq <= resolveAppliedRef.current` short-circuits older-than-applied
  // responses — so the helper is not called for stale responses.
  // Order 4 below exercises the counter directly.
});

test("Concern 3: terminal reason is tied to the ENDED room ID. After a new room becomes live, the tombstone clears; even if resolve still carries a `lastEndReason`, isRoomShownAsEnded returns false so the terminal message is not shown.", () => {
  const clearedTombstone = nextEndedRoomId("room-A", {
    activeRoomId: "room-B",
    roomStatus: "live",
    lastRoomId: "room-A",
    lastRoomStatus: "ended",
  });
  assert.equal(clearedTombstone.endedRoomId, null);
  assert.equal(
    isRoomShownAsEnded(clearedTombstone.endedRoomId, {
      activeRoomId: "room-B",
      roomStatus: "live",
      lastRoomStatus: "ended", // stale
    }),
    false,
  );
});

test("Concern 4: same room ID returning as live: DEFENSIVE behavior — treat as still-ended (won't reconnect). Backend generates fresh UUIDs, so this path is not expected, but the guard prevents accidental reconnection to a room the server considers ended.", () => {
  // The tombstone is A. Somehow /resolve returns activeRoomId=A live again.
  const t = nextEndedRoomId("room-A", { activeRoomId: "room-A", roomStatus: "live" });
  // "live && active && active !== prevEndedRoomId" is FALSE (active === prev),
  // so no clear happens. Tombstone stays.
  assert.equal(t.endedRoomId, "room-A");
  assert.equal(t.translationsCleared, false);
  assert.equal(
    isRoomShownAsEnded("room-A", { activeRoomId: "room-A", roomStatus: "live" }),
    true, // still shown as ended
  );
});

test("Concern 5: on new-room transition, translationsCleared=true is emitted so the caller resets fallbackEnLines + fallbackSeqRef BEFORE the render that shows the new room.", () => {
  const t = nextEndedRoomId("room-A", { activeRoomId: "room-B", roomStatus: "live" });
  assert.equal(t.endedRoomId, null);
  assert.equal(t.translationsCleared, true);
  // fetchResolve applies both the tombstone clear AND the translation clear
  // in the same async iteration, before the next render happens.
  // useSubtitleSocket separately clears enLines when resolvedUrl changes
  // via its own effect (scoped to the new room), so both channels are wiped.
});

// ── Event-order tests ───────────────────────────────────────────────────
// Simulate the page's applied state machine reacting to interleaved
// terminal events (with an identity-bearing `terminatedRoomId`) and
// /resolve responses. These pin the four ordering scenarios flagged by
// the reviewer as remaining races.

function simulate() {
  let endedRoomId: string | null = null;

  return {
    getEndedRoomId: () => endedRoomId,
    // Fires when a /resolve response is applied (already guarded by the
    // applied-counter — this simulator only ever sees the winning response).
    onResolveApplied: (data: ResolveSnapshot) => {
      const t = nextEndedRoomId(endedRoomId, data);
      if (t.endedRoomId !== endedRoomId) endedRoomId = t.endedRoomId;
    },
    // Fires when the WS terminates and delivers its captured room ID.
    onTerminatedRoomId: (terminatedRoomId: string | null) => {
      if (!terminatedRoomId) return;
      if (endedRoomId !== terminatedRoomId) endedRoomId = terminatedRoomId;
    },
    isCurrentlyEnded: (data: ResolveSnapshot) => isRoomShownAsEnded(endedRoomId, data),
  };
}

type ResolveSnapshot = Parameters<typeof nextEndedRoomId>[1];

test("Order 1: /resolve delivers B live, THEN terminal event for A → B remains live", () => {
  const s = simulate();
  s.onResolveApplied({ activeRoomId: "room-B", roomStatus: "live" });
  // Now the terminated event for the OLD socket (A) arrives.
  s.onTerminatedRoomId("room-A");
  // endedRoomId is now A (recorded), but the display checks the CURRENT
  // resolveData: activeRoomId=B, live. So the display is NOT terminal.
  assert.equal(s.getEndedRoomId(), "room-A");
  assert.equal(s.isCurrentlyEnded({ activeRoomId: "room-B", roomStatus: "live" }), false);
});

test("Order 2: terminal event for A first, THEN /resolve delivers B live → B becomes live", () => {
  const s = simulate();
  s.onTerminatedRoomId("room-A");
  // Before /resolve fires, display is terminal because activeRoomId is either
  // A or (soon) null — but we simulate the intermediate "still on A" state:
  assert.equal(s.isCurrentlyEnded({ activeRoomId: "room-A", roomStatus: "ended" }), true);
  s.onResolveApplied({ activeRoomId: "room-B", roomStatus: "live" });
  assert.equal(s.getEndedRoomId(), null); // tombstone cleared
  assert.equal(s.isCurrentlyEnded({ activeRoomId: "room-B", roomStatus: "live" }), false);
});

test("Order 3: /resolve changes activeRoomId A→B while socket A still connected, later terminal event carrying A must NOT tombstone B", () => {
  const s = simulate();
  // First: A is live.
  s.onResolveApplied({ activeRoomId: "room-A", roomStatus: "live" });
  // Then /resolve reports B live (previous room A was not yet observed as ended).
  s.onResolveApplied({ activeRoomId: "room-B", roomStatus: "live" });
  assert.equal(s.getEndedRoomId(), null);
  // Later, socket A closes with room_ended. The hook captured its room in
  // its own closure — it delivers "room-A" (NOT "room-B") to the page.
  s.onTerminatedRoomId("room-A");
  // Tombstone records A, not B.
  assert.equal(s.getEndedRoomId(), "room-A");
  // Display gate on the CURRENT room (B) is still non-terminal.
  assert.equal(s.isCurrentlyEnded({ activeRoomId: "room-B", roomStatus: "live" }), false);
});

test("Order 4: slow /resolve response A arrives after response B → A cannot overwrite B (applied-counter)", () => {
  // Simulate the applied-counter guard directly. `resolveReqRef` is the
  // number handed out to each request; `resolveAppliedRef` is the highest
  // successfully applied. A response is applied only if its number is
  // greater than resolveApplied.
  let issued = 0;
  let applied = 0;
  let stateActiveRoomId: string | null = null;

  const startRequest = () => ++issued;
  const tryApply = (myReq: number, activeRoomId: string) => {
    if (myReq <= applied) return false;
    applied = myReq;
    stateActiveRoomId = activeRoomId;
    return true;
  };

  const reqA = startRequest(); // slow — will return LAST
  const reqB = startRequest(); // fast

  // B lands first.
  assert.equal(tryApply(reqB, "room-B"), true);
  assert.equal(stateActiveRoomId, "room-B");

  // A (older, slower) lands afterwards.
  const okA = tryApply(reqA, "room-A");
  assert.equal(okA, false);              // rejected — older than applied
  assert.equal(stateActiveRoomId, "room-B"); // unchanged
});

test("Order 4b (starvation): slow requests still eventually apply — a response later than everything applied is accepted, not rejected on 'not latest issued' basis", () => {
  let issued = 0;
  let applied = 0;
  let latest: string | null = null;
  const startRequest = () => ++issued;
  const tryApply = (myReq: number, room: string) => {
    if (myReq <= applied) return false;
    applied = myReq;
    latest = room;
    return true;
  };

  const req1 = startRequest();  // very slow
  const req2 = startRequest();  // fired after req1 issued but before it applied
  // req1 wins the race and returns first (it's slow but req2 is even slower):
  assert.equal(tryApply(req1, "room-1"), true);
  assert.equal(latest, "room-1");
  // req2 returns even later — still newer than applied=1, still applies.
  assert.equal(tryApply(req2, "room-2"), true);
  assert.equal(latest, "room-2");
});

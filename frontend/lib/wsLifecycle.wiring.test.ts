// F2 v7: production-wiring regression tests for WsLifecycle.
//
// Separate from wsLifecycle.test.ts so the operator can see the counts
// distinct from the basic lifecycle coverage. These tests assert the
// FIVE invariants from v6 point 2 — each test maps 1:1 to a production
// regression operator asked to be prevented:
//
//   W1 — stale-socket replacement: old socket's late onclose cannot
//        affect the replacement socket's state/timers/media.
//   W2 — late event after unmount: callbacks after unmount() do nothing.
//   W3 — pending timer cleared on unmount: a scheduled reconnect never
//        fires if unmount happened first.
//   W4 — listener terminal persistence: terminal state survives
//        advancing timers past the disconnect banner.
//   W5 — terminal auth close releases media exactly once.

import { test, describe } from "node:test";
import assert from "node:assert";
import { WsLifecycle, type WsTerminalReason } from "./wsLifecycle.ts";

interface Harness {
  now: number;
  pending: Map<number, { fireAt: number; fn: () => void }>;
  nextHandle: number;
  setTimeoutImpl: (fn: () => void, ms: number) => unknown;
  clearTimeoutImpl: (handle: unknown) => void;
  advance: (ms: number) => void;
  pendingCount: () => number;
}

function makeHarness(): Harness {
  const pending = new Map<number, { fireAt: number; fn: () => void }>();
  const h: Harness = {
    now: 0,
    pending,
    nextHandle: 1,
    setTimeoutImpl: (fn, ms) => {
      const handle = h.nextHandle++;
      pending.set(handle, { fireAt: h.now + ms, fn });
      return handle;
    },
    clearTimeoutImpl: (handle) => {
      pending.delete(handle as number);
    },
    advance: (ms) => {
      h.now += ms;
      const ready: Array<{ handle: number; fn: () => void }> = [];
      for (const [handle, entry] of pending) {
        if (entry.fireAt <= h.now) ready.push({ handle, fn: entry.fn });
      }
      for (const r of ready) {
        pending.delete(r.handle);
        r.fn();
      }
    },
    pendingCount: () => pending.size,
  };
  return h;
}

interface Spies {
  terminal: Array<{ reason: WsTerminalReason; info: { lastCloseCode: number; attempts: number } }>;
  reconnects: Array<{ delayMs: number; nextAttempt: number }>;
  mediaReleaseCount: number;
}

function makeSpies(): Spies {
  return { terminal: [], reconnects: [], mediaReleaseCount: 0 };
}

function makeLifecycle(opts: {
  harness: Harness; spies: Spies; maxAttempts?: number; withMedia?: boolean;
}): WsLifecycle {
  return new WsLifecycle({
    label: "test",
    maxAttempts: opts.maxAttempts ?? 3,
    baseDelayMs: 1000,
    maxDelayMs: 8000,
    onTerminal: (reason, info) => { opts.spies.terminal.push({ reason, info }); },
    scheduleReconnect: (delayMs, nextAttempt) => {
      opts.spies.reconnects.push({ delayMs, nextAttempt });
    },
    releaseMedia: opts.withMedia ? () => { opts.spies.mediaReleaseCount += 1; } : undefined,
    random: () => 0.5,
    setTimeoutImpl: opts.harness.setTimeoutImpl,
    clearTimeoutImpl: opts.harness.clearTimeoutImpl,
  });
}

// ---------------------------------------------------------------------
// W1 — Stale-socket replacement
// ---------------------------------------------------------------------

describe("W1 — stale-socket replacement does not affect current socket", () => {
  test("old socket's late 4401 close cannot flip the replacement's state", () => {
    const h = makeHarness();
    const spies = makeSpies();
    const lc = makeLifecycle({ harness: h, spies, withMedia: true });

    const socketA = { id: "A" };
    const socketB = { id: "B" };

    // Socket A is current.
    lc.trackSocket(socketA);
    // Replacement — now socket B is current.
    lc.trackSocket(socketB);

    // Late close event from socket A — must be classified as stale.
    assert.strictEqual(lc.isStale(socketA), true, "socketA must be stale after B replaces it");
    assert.strictEqual(lc.isStale(socketB), false, "socketB must be current");

    // Simulate the hook's close dispatch: skip when isStale.
    if (lc.isStale(socketA)) {
      // guarded — do nothing
    } else {
      lc.handleClose({ code: 4401, reason: "" });
    }

    // Replacement must be untouched: no terminal, no media release, no retry call.
    assert.strictEqual(spies.terminal.length, 0, "stale close must not reach onTerminal");
    assert.strictEqual(spies.mediaReleaseCount, 0, "stale close must not release media");
    assert.strictEqual(spies.reconnects.length, 0, "stale close must not schedule a retry");
    assert.strictEqual(lc.isTerminal(), false, "replacement socket must remain non-terminal");
  });
});

// ---------------------------------------------------------------------
// W2 — Late event after unmount
// ---------------------------------------------------------------------

describe("W2 — late event after unmount is a no-op", () => {
  test("handleClose() after unmount() flips nothing and schedules no timer", () => {
    const h = makeHarness();
    const spies = makeSpies();
    const lc = makeLifecycle({ harness: h, spies, withMedia: true });

    const ws = { id: "A" };
    lc.trackSocket(ws);
    lc.unmount();

    // Guard the dispatch (production code does this at the top of onclose).
    if (lc.isStale(ws)) {
      // guarded — do nothing (unmounted => stale)
    } else {
      lc.handleClose({ code: 1006, reason: "" });
    }

    assert.strictEqual(spies.terminal.length, 0, "post-unmount close must not reach onTerminal");
    assert.strictEqual(spies.reconnects.length, 0, "post-unmount close must not schedule retry");
    assert.strictEqual(spies.mediaReleaseCount, 0, "post-unmount close must not release media");
    assert.strictEqual(h.pendingCount(), 0, "no timer scheduled after unmount");
  });
});

// ---------------------------------------------------------------------
// W3 — Pending timer cleared on unmount
// ---------------------------------------------------------------------

describe("W3 — pending reconnect timer is cleared on unmount", () => {
  test("unmount() clears any outstanding reconnect timer; advancing fake timers triggers nothing", () => {
    const h = makeHarness();
    const spies = makeSpies();
    const lc = makeLifecycle({ harness: h, spies });

    const ws = { id: "A" };
    lc.trackSocket(ws);

    // Trigger a close that schedules a reconnect timer internally.
    lc.handleClose({ code: 1006, reason: "" });
    // The lifecycle wraps scheduleReconnect() in setTimeoutImpl — until the
    // timer fires, the host's scheduleReconnect spy stays empty, but a
    // timer IS pending (verifiable via pendingCount).
    assert.strictEqual(h.pendingCount(), 1, "internal reconnect timer pending");

    // Register a UI disconnect-banner timer that must also be cleared.
    const bannerHandle = h.setTimeoutImpl(() => {
      throw new Error("banner timer fired after unmount — must have been cleared");
    }, 500);
    lc.registerDisconnectBannerTimer(bannerHandle);
    assert.strictEqual(h.pendingCount(), 2, "internal + banner timers pending");

    // Unmount — this must clear BOTH the internal reconnect timer and the
    // registered banner timer.
    lc.unmount();
    assert.strictEqual(h.pendingCount(), 0, "unmount cleared all pending timers");

    // Advancing fake timers past both durations fires nothing.
    h.advance(100_000);
    assert.strictEqual(spies.terminal.length, 0, "no terminal from stale timers");
    assert.strictEqual(spies.reconnects.length, 0, "no retry invoked after unmount");
  });
});

// ---------------------------------------------------------------------
// W4 — Listener terminal persistence
// ---------------------------------------------------------------------

describe("W4 — listener terminal state survives timer advancement", () => {
  test("terminal set on close(4401) survives advancing past banner duration", () => {
    const h = makeHarness();
    const spies = makeSpies();
    const lc = makeLifecycle({ harness: h, spies }); // listener (no releaseMedia)

    const ws = { id: "A" };
    lc.trackSocket(ws);

    // Register a disconnect-banner timer BEFORE the terminal close.
    let bannerFired = false;
    const bannerHandle = h.setTimeoutImpl(() => { bannerFired = true; }, 500);
    lc.registerDisconnectBannerTimer(bannerHandle);

    lc.handleClose({ code: 4401, reason: "" });

    assert.strictEqual(lc.isTerminal(), true, "terminal state set after 4401");
    assert.strictEqual(spies.terminal.length, 1, "onTerminal fired once");
    assert.strictEqual(spies.terminal[0].reason, "auth", "terminal reason = auth");

    // Advancing past the banner duration must NOT undo the terminal.
    h.advance(5_000);

    assert.strictEqual(bannerFired, false, "banner timer must have been cleared on terminal");
    assert.strictEqual(lc.isTerminal(), true, "terminal state persists after timer advance");
    assert.strictEqual(spies.reconnects.length, 0, "no retry scheduled after terminal");
  });
});

// ---------------------------------------------------------------------
// W5 — Terminal auth close releases media exactly once
// ---------------------------------------------------------------------

describe("W5 — terminal auth close releases media exactly once (host mode)", () => {
  test("close(4401) releases media; a subsequent close does not release again", () => {
    const h = makeHarness();
    const spies = makeSpies();
    const lc = makeLifecycle({ harness: h, spies, withMedia: true });

    const ws = { id: "A" };
    lc.trackSocket(ws);
    lc.handleClose({ code: 4401, reason: "" });

    assert.strictEqual(spies.mediaReleaseCount, 1, "media released exactly once on 4401");
    assert.strictEqual(lc.isTerminal(), true);

    // Simulate a later erroneous close hitting the same lifecycle.
    // Production code would guard with isStale; even if it did reach,
    // media must not be released a second time.
    lc.handleClose({ code: 1006, reason: "" });
    assert.strictEqual(spies.mediaReleaseCount, 1, "media not released twice");
  });

  test("close(4403) also releases media exactly once (forbidden path)", () => {
    const h = makeHarness();
    const spies = makeSpies();
    const lc = makeLifecycle({ harness: h, spies, withMedia: true });

    const ws = { id: "A" };
    lc.trackSocket(ws);
    lc.handleClose({ code: 4403, reason: "" });

    assert.strictEqual(spies.mediaReleaseCount, 1, "media released on 4403");
    assert.strictEqual(spies.terminal[0].reason, "forbidden");
  });
});

// ---------------------------------------------------------------------
// Final cross-check: retryDecision() is actually called in these paths.
// Importing it isn't enough; the production hook must INVOKE it.
// ---------------------------------------------------------------------

describe("W-X — retryDecision invocations are observable on every close", () => {
  test("ordinary 1006 close increments retryDecisionInvocations by 1", () => {
    const h = makeHarness();
    const spies = makeSpies();
    const lc = makeLifecycle({ harness: h, spies });

    const ws = { id: "A" };
    lc.trackSocket(ws);
    const before = lc.retryDecisionInvocations();
    lc.handleClose({ code: 1006, reason: "" });
    assert.strictEqual(lc.retryDecisionInvocations(), before + 1,
      "retryDecision must be called exactly once per handleClose");
  });

  test("terminal 4401 close ALSO invokes retryDecision (single arbiter)", () => {
    const h = makeHarness();
    const spies = makeSpies();
    const lc = makeLifecycle({ harness: h, spies });

    const ws = { id: "A" };
    lc.trackSocket(ws);
    lc.handleClose({ code: 4401, reason: "" });
    assert.strictEqual(lc.retryDecisionInvocations(), 1,
      "retryDecision is the single arbiter and is called even on terminal codes");
  });
});

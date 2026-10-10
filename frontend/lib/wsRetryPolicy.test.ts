import { test, describe } from "node:test";
import assert from "node:assert";
import { retryDecision, TERMINAL_CLOSE_CODES } from "./wsRetryPolicy.ts";

describe("retryDecision — F2 v4 four cases", () => {
  test("Case A: 10 consecutive handshake failures → stops after cap", () => {
    const MAX = 8;
    let attempt = 0;
    const decisions: ReturnType<typeof retryDecision>[] = [];
    for (let i = 0; i < 10; i++) {
      const d = retryDecision({
        attempt,
        maxAttempts: MAX,
        closeCode: 1006,
        closeReason: undefined,
      });
      decisions.push(d);
      if (d.kind !== "retry") break;
      attempt = d.nextAttempt;
    }
    // Last decision must be terminal with reason=exhausted.
    const last = decisions[decisions.length - 1];
    assert.equal(last.kind, "terminal");
    if (last.kind === "terminal") {
      assert.equal(last.reason, "exhausted");
    }
    // Count of retry decisions must equal MAX (8), then one terminal.
    const retries = decisions.filter((d) => d.kind === "retry").length;
    assert.equal(retries, MAX);
    assert.equal(decisions.length, MAX + 1);
  });

  test("Case B: 3 failures → successful byte reception (reset) → next failure retries fresh", () => {
    const MAX = 8;
    // 3 failures
    let attempt = 0;
    for (let i = 0; i < 3; i++) {
      const d = retryDecision({ attempt, maxAttempts: MAX, closeCode: 1006, closeReason: undefined });
      assert.equal(d.kind, "retry");
      if (d.kind === "retry") attempt = d.nextAttempt;
    }
    assert.equal(attempt, 3);

    // Successful byte reception — hook resets attempt to 0.
    attempt = 0;

    // Next failure retries fresh.
    const d = retryDecision({ attempt, maxAttempts: MAX, closeCode: 1006, closeReason: undefined });
    assert.equal(d.kind, "retry");
    if (d.kind === "retry") {
      assert.equal(d.nextAttempt, 1);
      // base delay from fresh counter is small — not escalated to the 8th-attempt cap.
      assert.equal(d.delayMs, 500);  // 1000 * 2^0 * 0.5 (jitter01=0 → 0.5)
    }
  });

  test("Case C: terminal 1000 room_ended → stops after one close, no retry", () => {
    const d = retryDecision({
      attempt: 0,
      maxAttempts: 8,
      closeCode: 1000,
      closeReason: "room_ended",
    });
    assert.equal(d.kind, "terminal");
    if (d.kind === "terminal") {
      assert.equal(d.reason, "roomEnded");
    }
  });

  test("Case C': terminal 1000 room_ended:sweeper variant also terminal", () => {
    const d = retryDecision({
      attempt: 2,
      maxAttempts: 8,
      closeCode: 1000,
      closeReason: "room_ended:sweeper",
    });
    assert.equal(d.kind, "terminal");
  });

  test("Case D: ordinary 1006 blip → retries with exponential backoff", () => {
    const MAX = 8;
    const delays: number[] = [];
    let attempt = 0;
    for (let i = 0; i < 5; i++) {
      const d = retryDecision({
        attempt,
        maxAttempts: MAX,
        closeCode: 1006,
        closeReason: undefined,
        baseDelayMs: 1000,
        maxDelayMs: 30000,
        jitter01: 0,  // deterministic
      });
      assert.equal(d.kind, "retry");
      if (d.kind === "retry") {
        delays.push(d.delayMs);
        attempt = d.nextAttempt;
      }
    }
    // Attempts 1..5 → base * 2^(0..4) * 0.5 (jitter=0 → 0.5 of range)
    // 1000 * 1 * 0.5, 1000 * 2 * 0.5, 1000 * 4 * 0.5, 1000 * 8 * 0.5, 1000 * 16 * 0.5
    assert.deepEqual(delays, [500, 1000, 2000, 4000, 8000]);
  });

  test("4401 auth-fail at any attempt → terminal reason=auth", () => {
    const d = retryDecision({
      attempt: 0,
      maxAttempts: 8,
      closeCode: 4401,
      closeReason: undefined,
    });
    assert.equal(d.kind, "terminal");
    if (d.kind === "terminal") {
      assert.equal(d.reason, "auth");
    }
  });

  test("4403 forbidden at any attempt → terminal reason=forbidden", () => {
    const d = retryDecision({
      attempt: 3,
      maxAttempts: 8,
      closeCode: 4403,
      closeReason: undefined,
    });
    assert.equal(d.kind, "terminal");
    if (d.kind === "terminal") {
      assert.equal(d.reason, "forbidden");
    }
  });

  test("TERMINAL_CLOSE_CODES set is the known set", () => {
    assert.ok(TERMINAL_CLOSE_CODES.has(4401));
    assert.ok(TERMINAL_CLOSE_CODES.has(4403));
    assert.ok(!TERMINAL_CLOSE_CODES.has(1006));
    assert.ok(!TERMINAL_CLOSE_CODES.has(1000));
  });

  test("delay caps at maxDelayMs even on high attempts", () => {
    // attempt=20 → 1000 * 2^20 would be ~1e9, but cap is 30000
    const d = retryDecision({
      attempt: 19,
      maxAttempts: 100,
      closeCode: 1006,
      closeReason: undefined,
      baseDelayMs: 1000,
      maxDelayMs: 30000,
      jitter01: 1,  // full jitter upper bound
    });
    assert.equal(d.kind, "retry");
    if (d.kind === "retry") {
      // Exponential: capped at 30000. jitter upper (jitter01=1) → full 30000.
      assert.equal(d.delayMs, 30000);
    }
  });

  test("non-1000 close with room_ended reason does NOT short-circuit as roomEnded", () => {
    // Only close code 1000 is terminal via reason prefix.
    const d = retryDecision({
      attempt: 0,
      maxAttempts: 8,
      closeCode: 1011,
      closeReason: "room_ended",
    });
    assert.equal(d.kind, "retry");
  });

  test("cap=0 means no retries at all", () => {
    const d = retryDecision({
      attempt: 0,
      maxAttempts: 0,
      closeCode: 1006,
      closeReason: undefined,
    });
    assert.equal(d.kind, "terminal");
    if (d.kind === "terminal") {
      assert.equal(d.reason, "exhausted");
    }
  });
});

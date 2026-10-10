// F2 v4: pure retry-policy functions, extracted from the WS hooks so
// they can be unit-tested in isolation without React or a WebSocket mock.
//
// Both frontend hooks (useTranslationSocket, useDeepgramProducer) now
// share this policy. Keeping the policy in one place removes the risk
// of them diverging as retry rules evolve.
//
// Four cases the tests verify (see wsRetryPolicy.test.ts):
//   A. 10 consecutive handshake failures → stops after MAX attempts
//   B. 3 failures → successful byte reception → counter resets →
//      next failure retries fresh
//   C. terminal close (room_ended) → stops immediately, no retry
//   D. ordinary 1006 blip → retries with exponential backoff
//
// Cleanup: the hook always clears its timer on unmount. That's a hook
// concern, not a policy concern — covered by the hook test itself.

export type RetryDecision =
  | { kind: "retry"; delayMs: number; nextAttempt: number }
  | { kind: "terminal"; reason: "exhausted" | "auth" | "forbidden" | "roomEnded" };

export interface RetryPolicyInput {
  attempt: number;        // 0-based count of PRIOR failed attempts
  maxAttempts: number;    // cap
  closeCode: number;      // WS close code observed
  closeReason: string | undefined;   // WS close reason string
  baseDelayMs?: number;   // default 1000
  maxDelayMs?: number;    // default 30000
  jitter01?: number;      // deterministic jitter in [0..1] for tests; default 0
}

// Terminal close codes — never retry.
export const TERMINAL_CLOSE_CODES = new Set<number>([4401, 4403]);

export function retryDecision(input: RetryPolicyInput): RetryDecision {
  const {
    attempt,
    maxAttempts,
    closeCode,
    closeReason,
    baseDelayMs = 1000,
    maxDelayMs = 30000,
    jitter01 = 0,
  } = input;

  // Terminal reject-before-accept (observed as 4401/4403 when the server
  // sends a close frame with a 4xxx code; browsers may surface this as
  // 1006 instead — the attempts cap handles the 1006 case).
  if (TERMINAL_CLOSE_CODES.has(closeCode)) {
    return { kind: "terminal", reason: closeCode === 4401 ? "auth" : "forbidden" };
  }

  // Terminal room closure.
  if (closeCode === 1000 && typeof closeReason === "string" && closeReason.startsWith("room_ended")) {
    return { kind: "terminal", reason: "roomEnded" };
  }

  const nextAttempt = attempt + 1;
  if (nextAttempt > maxAttempts) {
    return { kind: "terminal", reason: "exhausted" };
  }

  const exponential = Math.min(maxDelayMs, baseDelayMs * Math.pow(2, nextAttempt - 1));
  const jittered = Math.round(exponential * (0.5 + 0.5 * jitter01));
  return { kind: "retry", delayMs: jittered, nextAttempt };
}

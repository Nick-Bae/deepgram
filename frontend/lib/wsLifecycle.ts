// F2 v6: extracted WebSocket lifecycle controller shared by both hooks.
//
// Why this exists
// ---------------
// Both frontend WebSocket hooks (useTranslationSocket, useDeepgramProducer)
// repeat the same close-event classification, stale-socket guard, mount
// guard, timer management, and media-release-on-terminal logic. v5 shared
// only the pure `retryDecision` function; v6 shares the whole lifecycle
// so the actual production code is testable without React or jsdom.
//
// Correctness invariants this file enforces:
//
//   1. Stale-socket guard: a callback fired by an OLD socket (because a
//      replacement was created before the browser delivered the old
//      socket's late onclose/onerror/onmessage) MUST NOT mutate state
//      meant for the current socket. We check `socketRef.current ===
//      thisSocket` at the top of every callback.
//
//   2. Mount guard: a callback fired after the owning React effect has
//      been cleaned up MUST NOT mutate state, schedule timers, or
//      release media. We check `isMountedRef.current` at the top of
//      every callback.
//
//   3. Terminal persistence: on terminal (auth / forbidden / roomEnded
//      / exhausted) we CLEAR every pending timer the lifecycle owns
//      before setting the terminal state. Later close events in the
//      same terminal branch are no-ops.
//
//   4. retryDecision() is actually CALLED on every close. The import
//      is not decorative. The listener hook in v5 imported it but
//      used an ad-hoc policy inline; v6 routes every close through
//      this file so retryDecision() is the single arbiter.
//
//   5. Media release on terminal auth: on 4401/4403 the lifecycle
//      invokes the host-provided `releaseMedia` callback once, then
//      refuses further invocations.
//
// Zero React. Zero DOM. Pure TypeScript with injected dependencies —
// trivial to test with `node --test --experimental-strip-types`.

import { retryDecision, type RetryDecision } from "./wsRetryPolicy.ts";

export type WsTerminalReason = "auth" | "forbidden" | "roomEnded" | "exhausted";

export interface WsLifecycleOptions {
  /** Human label for logs. */
  label: string;
  /** Max retry attempts for the exhaustion branch. */
  maxAttempts: number;
  /** Base delay fed into retryDecision. */
  baseDelayMs?: number;
  /** Max delay cap fed into retryDecision. */
  maxDelayMs?: number;
  /** Called when a terminal state is reached (ONE time per lifecycle). */
  onTerminal: (reason: WsTerminalReason, info: { lastCloseCode: number; attempts: number }) => void;
  /** Called when a retry is scheduled; implementation starts the connect flow. */
  scheduleReconnect: (delayMs: number, nextAttempt: number) => void;
  /** Called ONCE on first terminal auth/forbidden to release the media pipeline. Omit for listener. */
  releaseMedia?: () => void;
  /** Injectable Math.random for deterministic testing. */
  random?: () => number;
  /** Injectable timer API for deterministic testing. */
  setTimeoutImpl?: (fn: () => void, ms: number) => unknown;
  clearTimeoutImpl?: (handle: unknown) => void;
}

export class WsLifecycle {
  readonly label: string;
  readonly maxAttempts: number;
  private readonly baseDelayMs: number;
  private readonly maxDelayMs: number;
  private readonly onTerminal: WsLifecycleOptions["onTerminal"];
  private readonly scheduleReconnect: WsLifecycleOptions["scheduleReconnect"];
  private readonly releaseMedia?: () => void;
  private readonly random: () => number;
  private readonly setTimeoutImpl: (fn: () => void, ms: number) => unknown;
  private readonly clearTimeoutImpl: (handle: unknown) => void;

  // Mutable per-lifecycle state. Each `start()` of a new socket should
  // call `trackSocket(ws)` first; every callback MUST call `isStale(ws)`
  // first and bail if true.
  private currentSocket: unknown = null;
  private mounted = true;
  private attempts = 0;
  private reconnectTimerHandle: unknown = null;
  private disconnectBannerTimerHandle: unknown = null;
  private terminalReached = false;
  private mediaReleasedOnTerminal = false;
  private retryDecisionCallCount = 0;
  private lastCloseCode = 1006;

  constructor(opts: WsLifecycleOptions) {
    this.label = opts.label;
    this.maxAttempts = opts.maxAttempts;
    this.baseDelayMs = opts.baseDelayMs ?? 1000;
    this.maxDelayMs = opts.maxDelayMs ?? 30000;
    this.onTerminal = opts.onTerminal;
    this.scheduleReconnect = opts.scheduleReconnect;
    this.releaseMedia = opts.releaseMedia;
    this.random = opts.random ?? Math.random;
    this.setTimeoutImpl = opts.setTimeoutImpl ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimeoutImpl = opts.clearTimeoutImpl ?? ((h) => clearTimeout(h as ReturnType<typeof setTimeout>));
  }

  /** Associate a freshly-created WebSocket with this lifecycle. */
  trackSocket(ws: unknown): void {
    this.currentSocket = ws;
  }

  /** Called from the owning React effect's cleanup. */
  unmount(): void {
    this.mounted = false;
    this.clearReconnectTimer();
    this.clearDisconnectBannerTimer();
  }

  /** True if the callback that fired is for an old or null socket. */
  isStale(ws: unknown): boolean {
    return !this.mounted || this.currentSocket !== ws;
  }

  /** True if the lifecycle has reached a terminal state. */
  isTerminal(): boolean {
    return this.terminalReached;
  }

  /** Returns the current retry attempt count (for assertions / state surfacing). */
  attemptCount(): number {
    return this.attempts;
  }

  /** For tests: count of actual retryDecision() invocations. */
  retryDecisionInvocations(): number {
    return this.retryDecisionCallCount;
  }

  /** Reset attempts after confirmed byte reception. */
  recordBytesReceived(): void {
    if (!this.mounted || this.terminalReached) return;
    this.attempts = 0;
  }

  /** Register a UI banner timer that must be cleared on terminal. */
  registerDisconnectBannerTimer(handle: unknown): void {
    this.disconnectBannerTimerHandle = handle;
  }

  private clearReconnectTimer(): void {
    if (this.reconnectTimerHandle !== null) {
      this.clearTimeoutImpl(this.reconnectTimerHandle);
      this.reconnectTimerHandle = null;
    }
  }

  private clearDisconnectBannerTimer(): void {
    if (this.disconnectBannerTimerHandle !== null) {
      this.clearTimeoutImpl(this.disconnectBannerTimerHandle);
      this.disconnectBannerTimerHandle = null;
    }
  }

  /**
   * Classify a close event. Called from the hook's onclose callback
   * AFTER the stale/mount guards have passed. Routes every close through
   * retryDecision() — the single arbiter.
   *
   * Returns the retry decision for logging/testing; the lifecycle has
   * already acted on it (scheduling reconnect OR invoking onTerminal).
   */
  handleClose(event: { code: number; reason: string | undefined }): RetryDecision {
    this.lastCloseCode = event.code || 1006;
    if (this.terminalReached) {
      // Late onclose from an old socket in a terminal lifecycle — never
      // re-enter terminal, never retry.
      return { kind: "terminal", reason: "exhausted" };
    }
    this.retryDecisionCallCount += 1;
    const decision = retryDecision({
      attempt: this.attempts,
      maxAttempts: this.maxAttempts,
      closeCode: event.code,
      closeReason: event.reason,
      baseDelayMs: this.baseDelayMs,
      maxDelayMs: this.maxDelayMs,
      jitter01: this.random(),
    });
    if (decision.kind === "terminal") {
      this.enterTerminal(decision.reason);
      return decision;
    }
    this.attempts = decision.nextAttempt;
    this.reconnectTimerHandle = this.setTimeoutImpl(() => {
      this.reconnectTimerHandle = null;
      if (!this.mounted || this.terminalReached) return;
      this.scheduleReconnect(decision.delayMs, decision.nextAttempt);
    }, decision.delayMs);
    return decision;
  }

  private enterTerminal(reason: WsTerminalReason): void {
    this.terminalReached = true;
    this.clearReconnectTimer();
    this.clearDisconnectBannerTimer();
    if ((reason === "auth" || reason === "forbidden") && this.releaseMedia && !this.mediaReleasedOnTerminal) {
      this.mediaReleasedOnTerminal = true;
      try { this.releaseMedia(); } catch { /* swallow — media teardown must not re-enter */ }
    }
    try {
      this.onTerminal(reason, { lastCloseCode: this.lastCloseCode, attempts: this.attempts });
    } catch { /* swallow — do not re-enter terminal from a user callback error */ }
  }
}

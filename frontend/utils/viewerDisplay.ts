// Pure display-precedence logic for the listener page.
//
// Extracted so it can be unit-tested without a React or DOM environment.
// The rule this file enforces:
//
//   Terminal state (`serviceEnded` OR `socketTerminated`) MUST take
//   precedence over any lingering translation lines. Without this, a
//   viewer whose enLines already contain translated text keeps showing
//   the last sentence after End Service instead of "Broadcast ended.".

export type ViewerDisplayInputs = {
  serviceEnded: boolean;
  socketTerminated: boolean;
  displayEnLines: readonly string[];
  waitingMessage: string;
  lastEndReason?: string | null;
};

export type ViewerDisplayOutputs = {
  currentEn: string;
  recentEn: readonly string[];
  isTerminal: boolean;
};

export function roomEndMessage(reason?: string | null): string | null {
  switch ((reason || "").trim()) {
    case "trial_expired":
      return "Broadcast stopped: trial minutes exhausted.";
    case "monthly_limit_reached":
      return "Broadcast stopped: monthly limit reached.";
    case "host_absent":
      return "Broadcast stopped: host connection was lost.";
    case "idle_timeout":
      return "Broadcast stopped: no audio was detected.";
    case "max_duration":
      return "Broadcast stopped: maximum broadcast duration reached.";
    case "host_end":
      return "Broadcast ended.";
    default:
      return null;
  }
}

export function resolveViewerDisplay(input: ViewerDisplayInputs): ViewerDisplayOutputs {
  const {
    serviceEnded,
    socketTerminated,
    displayEnLines,
    waitingMessage,
    lastEndReason,
  } = input;

  const isTerminal = serviceEnded || socketTerminated;
  const terminalMessage = roomEndMessage(lastEndReason) || "Broadcast ended.";
  const lastEn = displayEnLines[displayEnLines.length - 1] || "";

  const currentEn = isTerminal
    ? terminalMessage
    : lastEn || waitingMessage;

  const recentEn = isTerminal ? [] : displayEnLines.slice(0, -1).slice(-2);

  return { currentEn, recentEn, isTerminal };
}

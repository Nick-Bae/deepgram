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

// Subtitle mode iterates a lines array (last one styled as the "current"
// large line). When the room has ended, collapse to just the terminal
// message so lingering translations don't mask "Broadcast ended.".
// Extracted so the collapse behavior is directly unit-tested — the bug
// this file was created to fix reappeared once because only fullscreen
// mode consumed resolveViewerDisplay's output.
export function subtitleModeLines(
  isTerminal: boolean,
  currentEn: string,
  displayEnLines: readonly string[],
): readonly string[] {
  return isTerminal ? [currentEn] : displayEnLines;
}

// Whether the socket-termination signal belongs to the ROOM the viewer is
// currently looking at. A stale terminal event captured for room A must
// not paint "Broadcast ended." over a live room B — the room-scoped
// tombstone (endedRoomId) drives future polls, but the immediate display
// gate has to be scoped too, or the very next render after A's socket
// closes shows terminal for whatever room /resolve currently reports.
export type SocketTerminatedScopeInputs = {
  socketTerminated: boolean;
  socketTerminatedRoomId: string | null;
  activeRoomId: string | null;
  lastRoomId: string | null;
};

export function socketTerminatedForCurrentRoom(
  input: SocketTerminatedScopeInputs,
): boolean {
  if (!input.socketTerminated || !input.socketTerminatedRoomId) return false;
  if (input.socketTerminatedRoomId === input.activeRoomId) return true;
  if (!input.activeRoomId && input.socketTerminatedRoomId === input.lastRoomId) {
    return true;
  }
  return false;
}

// The `lastEndReason` in a /resolve payload always refers to the most
// recent ended room in the backend view. If the viewer's tombstone points
// at a different room (e.g. we terminated via the WS close before /resolve
// caught up, or /resolve is describing a newer, unrelated ended room),
// applying that reason string would show a specific message like
// "Broadcast stopped: trial minutes exhausted." for a room that in fact
// ended for another reason. Fall back to the generic "Broadcast ended."
// unless the reason clearly belongs to the tombstoned room.
export type EndReasonScopeInputs = {
  endedRoomId: string | null;
  activeRoomId: string | null;
  roomStatus: string;
  lastRoomId: string | null;
  lastRoomStatus: string;
  lastEndReason: string | null | undefined;
};

export function endReasonForEndedRoom(
  input: EndReasonScopeInputs,
): string | null {
  if (!input.endedRoomId) return null;
  const activeIsEnded =
    input.roomStatus === "ended" && input.activeRoomId === input.endedRoomId;
  const lastIsEnded =
    input.lastRoomStatus === "ended" && input.lastRoomId === input.endedRoomId;
  if (activeIsEnded || lastIsEnded) return input.lastEndReason ?? null;
  return null;
}

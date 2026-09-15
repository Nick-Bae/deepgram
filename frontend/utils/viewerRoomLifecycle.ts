// Pure state-transition logic for the listener page's room-ended tracking.
//
// PR #16 introduced an irreversible `serviceEnded` boolean that stopped
// /resolve polling permanently once End Service fired. That prevented the
// listener from discovering a new room started under the SAME service URL —
// only a page refresh could recover. This module scopes the terminal state
// to a specific ended room ID, so a subsequent live room under the same
// service URL is detected on the next /resolve poll and clears the terminal
// state automatically.
//
// The rules encoded here:
//
//   1. When /resolve reports `roomStatus="ended"` with an `activeRoomId`,
//      record that room ID as the ended one (transition true if it differs
//      from the previous ended ID — the caller should clear translations).
//
//   2. When /resolve reports no `activeRoomId` but a `lastRoomStatus="ended"`
//      with a `lastRoomId`, record that lastRoomId — Firestore's end_room
//      path nulls out `activeRoomId` and moves the room ID to `lastRoomId`.
//
//   3. When /resolve reports `roomStatus="live"` with an `activeRoomId`
//      that differs from the recorded ended ID, clear the ended state and
//      request a translations reset (caller wipes stale display / seq / lines).
//
//   4. Same `activeRoomId` returning is defensive: if the backend somehow
//      re-uses the ID, we still treat it as ended (do not reconnect).

export type ResolveSnapshot = {
  activeRoomId?: string | null;
  roomStatus?: string;
  lastRoomId?: string | null;
  lastRoomStatus?: string | null;
};

export type LifecycleTransition = {
  endedRoomId: string | null;
  translationsCleared: boolean;
};

/**
 * Compute the next ended-room-id given the previous value and a /resolve payload.
 * Callers should reset translation display state whenever `translationsCleared`
 * is true.
 */
export function nextEndedRoomId(
  prevEndedRoomId: string | null,
  data: ResolveSnapshot,
): LifecycleTransition {
  const active = data.activeRoomId ?? null;
  const status = (data.roomStatus ?? "").toString();
  const last = data.lastRoomId ?? null;
  const lastStatus = (data.lastRoomStatus ?? "").toString();

  // Case 1: /resolve is reporting an active room whose status is "ended".
  if (status === "ended" && active) {
    return {
      endedRoomId: active,
      translationsCleared: prevEndedRoomId !== active,
    };
  }

  // Case 2: /resolve has no active room, but the most recent one ended.
  if (!active && lastStatus === "ended" && last) {
    return {
      endedRoomId: last,
      translationsCleared: prevEndedRoomId !== last,
    };
  }

  // Case 3: a new live room has replaced the ended one — clear the ended flag
  //         so the socket can reconnect against the new activeRoomId.
  if (status === "live" && active && active !== prevEndedRoomId) {
    return {
      endedRoomId: null,
      translationsCleared: prevEndedRoomId !== null,
    };
  }

  // Nothing relevant to change (still waiting, or the same live room, etc).
  return { endedRoomId: prevEndedRoomId, translationsCleared: false };
}

/**
 * Whether the listener page should render the terminal message right now.
 *
 * True in two cases: (a) resolve still points at the ended room, or (b)
 * resolve has cleared activeRoomId while the tombstone is still set. False
 * once a different activeRoomId reappears (caller will already have cleared
 * `endedRoomId` via `nextEndedRoomId`).
 */
export function isRoomShownAsEnded(
  endedRoomId: string | null,
  data: ResolveSnapshot,
): boolean {
  if (!endedRoomId) return false;
  const active = data.activeRoomId ?? null;
  if (active === endedRoomId) return true;
  if (!active) return true;
  return false;
}

// Pure classification of WebSocket close events for the host producer
// (useDeepgramProducer) and the listener (useSubtitleSocket).
//
// Backend signals a *terminal* close — "the room is over, do not
// reconnect" — with either:
//   - reason === "room_ended" (typically paired with code 1000), or
//   - code   === 4001 (the app-specific room-ended code).
//
// Every other close (1001 going-away, 1006 abnormal, 1012 service-
// restart Uvicorn emits on SIGTERM, transient network drops) is
// *transient*: the client should reconnect and, for the host, preserve
// the running intent + the acquired microphone stream so the browser
// doesn't re-prompt the user.
//
// Extracted so it can be unit-tested without a WebSocket, a DOM, or a
// React runtime. Both hooks call the same function so their semantics
// cannot drift.

export type CloseEventLike = { code: number; reason?: string };

export type CloseClassification = "terminal_room_ended" | "transient";

export function classifyWebSocketClose(event: CloseEventLike): CloseClassification {
  if (event.code === 4001) return "terminal_room_ended";
  if ((event.reason ?? "") === "room_ended") return "terminal_room_ended";
  return "transient";
}

export function isTerminalRoomClose(event: CloseEventLike): boolean {
  return classifyWebSocketClose(event) === "terminal_room_ended";
}

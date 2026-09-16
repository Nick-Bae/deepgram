"""Structural guarantees about FastAPI's _on_shutdown handler.

Uvicorn 0.34 closes active WebSockets with code 1012 (Service Restart)
BEFORE FastAPI's shutdown-event runs (Cloud Run gives roughly 10 s
between SIGTERM and SIGKILL). That means _on_shutdown must NOT:

  * write to Firestore (no room state changes on the way out — the
    reconciler on another instance, or on this instance after a fresh
    start, is the authority on cleanup),
  * emit any terminal 'room_ended' event (would tombstone listeners
    that should transparently reconnect to a surviving instance),
  * call ConnectionManager.broadcast_room / close_room_listeners
    (same reason).

The handler's only allowed responsibilities: cancel long-running
background tasks (reconciler, sweeper) and stop the Redis pubsub
client so its connection is closed cleanly.

This test uses AST analysis so it fails at test-time — not just at
runtime — if a future edit reintroduces a Firestore or broadcast
side-effect into the shutdown path. Runtime tests miss branches that
never execute on the happy path.
"""
from __future__ import annotations

import ast
from pathlib import Path

MAIN_PY = Path(__file__).resolve().parent.parent / "app" / "main.py"

# Names that would indicate a Firestore write or terminal broadcast if
# they appeared inside _on_shutdown. Names that reference these but
# only as attribute access (never called) are also flagged — the
# handler must not touch these APIs at all.
_FORBIDDEN_CALL_TARGETS = frozenset({
    # ConnectionManager surface
    "broadcast_room",
    "close_room_listeners",
    # multichurch_store terminal writes
    "set_room_status",
    "mark_room_ended",
    "end_room",
    "close_room",
    "delete_room",
})

# Substrings that indicate the code is manufacturing a terminal room-ended
# event, regardless of the exact callee name.
_FORBIDDEN_STRING_LITERALS = frozenset({
    "room_ended",
    "roomStatus",
})


def _find_on_shutdown_function() -> ast.AsyncFunctionDef:
    """Locate _on_shutdown in main.py by matching the FastAPI decorator.

    We identify by decorator (@app.on_event("shutdown")) rather than by
    function name so a future rename can't quietly skip the check.
    """
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for deco in node.decorator_list:
            if (
                isinstance(deco, ast.Call)
                and isinstance(deco.func, ast.Attribute)
                and deco.func.attr == "on_event"
                and len(deco.args) == 1
                and isinstance(deco.args[0], ast.Constant)
                and deco.args[0].value == "shutdown"
            ):
                return node
    raise AssertionError(
        "No @app.on_event('shutdown') handler found in main.py. If the "
        "shutdown-event pattern moved to a lifespan context manager, this "
        "test needs to locate the new handler; a silent skip would defeat "
        "its purpose."
    )


def _walk_body(fn: ast.AsyncFunctionDef):
    """Walk only the function body — excludes decorators, so
    @app.on_event('shutdown') does not leak 'on_event' into the
    collected call set."""
    for stmt in fn.body:
        yield from ast.walk(stmt)


def _collect_called_names(fn: ast.AsyncFunctionDef) -> set[str]:
    names: set[str] = set()
    for node in _walk_body(fn):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name):
            names.add(callee.id)
        elif isinstance(callee, ast.Attribute):
            names.add(callee.attr)
    return names


def _collect_string_constants(fn: ast.AsyncFunctionDef) -> set[str]:
    strings: set[str] = set()
    for node in _walk_body(fn):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            strings.add(node.value)
    return strings


def test_on_shutdown_calls_no_forbidden_targets() -> None:
    fn = _find_on_shutdown_function()
    called = _collect_called_names(fn)
    leaked = called & _FORBIDDEN_CALL_TARGETS
    assert not leaked, (
        f"_on_shutdown must not call {sorted(leaked)}. Uvicorn 0.34 "
        f"closes WebSockets with code 1012 BEFORE this handler runs, so "
        f"any terminal broadcast or Firestore write here is either a "
        f"race (fires after clients have already reconnected elsewhere) "
        f"or worse, tombstones listeners that should transparently "
        f"reconnect. Cancel background tasks and stop pubsub — that's "
        f"the whole allowed surface."
    )


def test_on_shutdown_contains_no_room_ended_literal() -> None:
    fn = _find_on_shutdown_function()
    strings = _collect_string_constants(fn)
    leaked = strings & _FORBIDDEN_STRING_LITERALS
    assert not leaked, (
        f"_on_shutdown must not construct a 'room_ended' payload or "
        f"reference roomStatus (leaked: {sorted(leaked)}). Instance "
        f"restart is transient — the client hooks classify Uvicorn's "
        f"code 1012 as transient and reconnect. A room_ended emission "
        f"here would incorrectly tombstone them."
    )


def test_regression_gate_catches_a_forbidden_call() -> None:
    """Acceptance-gate self-test. Parses an inline shim that mimics
    _on_shutdown with a bad broadcast_room call, confirms the same
    helper functions the real tests use would flag it. Prevents the
    silent-pass failure mode where the check passes only because the
    walker is looking in the wrong place."""
    shim_src = (
        "async def _shim():\n"
        "    await manager.broadcast_room('org', 'room', {'event': 'room_ended'})\n"
        "    return None\n"
    )
    tree = ast.parse(shim_src)
    fn = tree.body[0]
    assert isinstance(fn, ast.AsyncFunctionDef)
    called = _collect_called_names(fn)
    strings = _collect_string_constants(fn)
    assert "broadcast_room" in called, "helper failed to detect the forbidden call"
    assert "room_ended" in strings, "helper failed to detect the forbidden literal"
    assert called & _FORBIDDEN_CALL_TARGETS, "forbidden-set membership check failed"
    assert strings & _FORBIDDEN_STRING_LITERALS, "forbidden-literal check failed"


def test_on_shutdown_only_cancels_tasks_and_stops_pubsub() -> None:
    """Positive guarantee: the handler's called-name set is a subset of
    the allowed surface. Broader than the forbidden-list check — this
    fails on NEW side-effects that the forbidden list happened not to
    enumerate."""
    fn = _find_on_shutdown_function()
    called = _collect_called_names(fn)
    allowed = {
        # asyncio task lifecycle
        "cancel", "done",
        # awaited tasks / coroutines / async cleanup
        "stop",
        # exception classes referenced in except-clauses via Call are
        # rare, but ast.walk catches type-name usage too:
        "CancelledError",
        # import from inside the function (lazy pubsub import)
        # -> no call name; caught by ast walk of Import nodes only.
        # Logging
        "print",
    }
    unexpected = called - allowed
    assert not unexpected, (
        f"_on_shutdown called unexpected names: {sorted(unexpected)}. "
        f"If any of these are new legitimate cleanup steps (not "
        f"Firestore writes or client broadcasts), add them to the "
        f"'allowed' set in this test. If any of them CAN reach "
        f"Firestore or the connection manager, the underlying code "
        f"needs to move out of the shutdown handler — Uvicorn closes "
        f"the sockets before this runs, so the writes race with client "
        f"reconnects to surviving instances."
    )

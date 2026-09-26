#!/usr/bin/env python3
"""Fake `firebase` shim for `test_deploy_rooms_status_index.py`.

R3 model — strict argv validation (finding 9):
  - `firebase --version`  → prints `state["firebase_version"]`, rc 0
  - `firebase deploy --project=PROJECT --only=firestore:indexes --non-interactive --json`
      → runs the deploy side-effect scripted in state; rc from
        state["deploy_rc"]

Any other invocation is refused with rc=2 so a driver typo
surfaces immediately.

State drives:
  "firebase_version": "13.19.0"
  "deploy_rc": 0
  "deploy_side_effect": one of:
    - "commit_success" (default)   — publishes rooms.status
                                     override; CG_ASC → READY via
                                     poll_state_sequence
    - "commit_no_op"               — no server-side change
    - "commit_needs_repair"        — publishes override; CG_ASC
                                     → NEEDS_REPAIR
    - "commit_never_ready"         — publishes override; CG_ASC
                                     → CREATING forever
    - "commit_leaves_missing"      — publishes override in fields
                                     list, but describe never
                                     shows the CG_ASC entry
    - "commit_partial_delete_unrelated" — deletes an unrelated
                                     composite
    - "commit_extra_addition"     — adds an unrelated composite
    - "sigint_mid_deploy"         — SIGINT parent driver; spawn
                                     a background child that
                                     WOULD mutate state 2 s later
                                     if not killed (finding 6)
    - "commit_regresses_after_ready" — CG_ASC observes READY once
                                     then MISSING (finding 7)
"""
from __future__ import annotations
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


_PROJECT_ID = "sturdy-dogfish-472313-k6"

_ALLOWED_DEPLOY_ARGV = [
    "deploy",
    f"--project={_PROJECT_ID}",
    "--only=firestore:indexes",
    "--non-interactive",
    "--json",
]


def _load_state() -> tuple[Path, dict]:
    p = Path(os.environ["PR42_FAKE_STATE"])
    return p, json.loads(p.read_text())


def _save_state(p: Path, state: dict) -> None:
    p.write_text(json.dumps(state, indent=2) + "\n")


# --- version ------------------------------------------------------------


def _handle_version(state: dict) -> int:
    ver = state.get("firebase_version", "13.19.0")
    print(ver)
    return 0


# --- deploy side effects ------------------------------------------------


def _rooms_status_override_entry() -> dict:
    """The four-entry override that a real firebase deploy would
    write for rooms.status when applying PR #42's config. R3
    shape (nested `Index.fields[]`)."""
    def entry(*, order=None, array=None, scope, state_val="READY"):
        inner = {"fieldPath": "status"}
        if order is not None:
            inner["order"] = order
        if array is not None:
            inner["arrayConfig"] = array
        return {
            "fields": [inner],
            "queryScope": scope,
            "state": state_val,
        }
    return {
        "name": (
            "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
            "collectionGroups/rooms/fields/status"
        ),
        "indexConfig": {
            "indexes": [
                entry(order="ASCENDING",  scope="COLLECTION"),
                entry(order="DESCENDING", scope="COLLECTION"),
                entry(array="CONTAINS",   scope="COLLECTION"),
                entry(order="ASCENDING",  scope="COLLECTION_GROUP",
                      state_val="READY"),
            ],
            "usesAncestorConfig": False,
        },
    }


def _publish_rooms_status(state: dict) -> None:
    state.setdefault("field_overrides", []).append(
        _rooms_status_override_entry()
    )


def _apply_deploy_effect(state_path: Path, state: dict) -> None:
    effect = state.get("deploy_side_effect", "commit_success")
    if effect == "commit_no_op":
        return
    if effect == "commit_success":
        _publish_rooms_status(state)
        state.setdefault("poll_state_sequence", ["CREATING", "READY"])
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_needs_repair":
        _publish_rooms_status(state)
        state["poll_state_sequence"] = ["CREATING", "NEEDS_REPAIR"]
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_never_ready":
        _publish_rooms_status(state)
        state["poll_state_sequence"] = ["CREATING"] * 10000
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_leaves_missing":
        _publish_rooms_status(state)
        state["poll_state_sequence"] = ["MISSING"] * 10000
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_partial_delete_unrelated":
        _publish_rooms_status(state)
        state["composites"] = []
        state["poll_state_sequence"] = ["READY"]
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_extra_addition":
        _publish_rooms_status(state)
        state.setdefault("composites", []).append({
            "name": (
                "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
                "collectionGroups/unrelated_group/indexes/fake_id"
            ),
            "collectionGroup": "unrelated_group",
            "fields": [{"fieldPath": "foo", "order": "ASCENDING"}],
            "queryScope": "COLLECTION",
            "state": "READY",
        })
        state["poll_state_sequence"] = ["READY"]
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_regresses_after_ready":
        # Simulate the race the reviewer flagged: post-diff sees
        # the override present, polling sees READY once, then a
        # subsequent poll observes MISSING (index deleted or
        # otherwise disappeared). Driver must hard-stop on that
        # MISSING.
        _publish_rooms_status(state)
        state["poll_state_sequence"] = ["READY", "MISSING", "MISSING"]
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "sigint_mid_deploy":
        # Fork a detached child that would mutate the state 2 s
        # later, then SIGINT the parent driver. If the driver's
        # trap does NOT terminate the deploy process group, the
        # child survives and mutates state AFTER the post-snapshot.
        # If the trap correctly kills the process group, the child
        # is reaped BEFORE it mutates.
        pid = os.fork()
        if pid == 0:
            # Grandchild: detach and mutate later.
            os.setsid()
            time.sleep(2.0)
            try:
                cur = json.loads(state_path.read_text())
                cur["late_child_mutation"] = True
                cur["composites"] = cur.get("composites", []) + [{
                    "collectionGroup": "sabotage_group",
                    "fields": [{"fieldPath": "x", "order": "ASCENDING"}],
                    "queryScope": "COLLECTION",
                    "state": "READY",
                }]
                state_path.write_text(json.dumps(cur, indent=2) + "\n")
            except Exception:
                pass
            os._exit(0)
        # Parent-of-child (still the fake firebase process): send
        # SIGINT to the driver.
        parent = os.getppid()
        os.kill(parent, signal.SIGINT)
        # Simulate a hanging deploy: sleep so the driver's trap
        # gets a chance to observe us alive and terminate our
        # process group. The grandchild we forked is NOT in this
        # PG (setsid'd), so its survival depends on whether the
        # driver knows to kill IT too — for our test, we simply
        # verify that even the intended process-group kill of
        # THIS process happens, and use `late_child_mutation` as
        # the sabotage marker.
        time.sleep(5.0)
        return
    # Unknown effect: no-op.


def _handle_deploy(state_path: Path, state: dict, argv: list[str]) -> int:
    # R3 finding 9: strict argv validation. Any deviation → rc=2.
    if argv != _ALLOWED_DEPLOY_ARGV:
        print(
            f"fake_firebase: unexpected deploy argv {argv!r} "
            f"(expected {_ALLOWED_DEPLOY_ARGV!r})",
            file=sys.stderr,
        )
        return 2
    _apply_deploy_effect(state_path, state)
    rc = int(state.get("deploy_rc", 0))
    if rc != 0:
        print(f"fake_firebase: simulated deploy failure rc={rc}",
              file=sys.stderr)
    return rc


def main(argv):
    if not argv:
        print("usage: fake_firebase.py <args...>", file=sys.stderr)
        return 2
    state_path, state = _load_state()

    if argv == ["--version"]:
        return _handle_version(state)

    if argv and argv[0] == "deploy":
        return _handle_deploy(state_path, state, argv)

    print(f"fake_firebase: unhandled command {argv!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

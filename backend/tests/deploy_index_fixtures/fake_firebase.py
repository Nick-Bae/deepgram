#!/usr/bin/env python3
"""Fake `firebase` shim used by `test_deploy_rooms_status_index.py`.

Reads its scripted behavior from `$PR42_FAKE_STATE` (same file
`fake_gcloud.py` uses). Recognized top-level state keys used
here:

  "firebase_version": "13.19.0"     # returned by `firebase --version`
  "deploy_rc":        0             # rc for `firebase deploy`
  "deploy_side_effect": "commit_success"  # or "commit_no_op",
                                          # "sigint_mid_deploy"

`deploy_side_effect: "commit_success"` also advances the state
file so subsequent `fake_gcloud` snapshot calls see the deploy's
effect on Firestore (adds the CG_ASC entry). It does this by
rewriting `poll_state_sequence` (if not already set) to
["CREATING","READY"] and marking a `deploy_committed=true` flag.
"""
from __future__ import annotations
import json
import os
import signal
import sys
import time
from pathlib import Path


def _load_state() -> tuple[Path, dict]:
    p = Path(os.environ["PR42_FAKE_STATE"])
    return p, json.loads(p.read_text())


def _save_state(p: Path, state: dict) -> None:
    p.write_text(json.dumps(state, indent=2) + "\n")


def _handle_version(state: dict) -> int:
    ver = state.get("firebase_version", "13.19.0")
    print(ver)
    return 0


def _publish_new_rooms_status_override(state: dict) -> None:
    """Simulate what `firebase deploy --only firestore:indexes`
    would do to production: create the rooms.status field
    override with the four intended entries so a subsequent
    `gcloud firestore indexes fields list --collection-group=rooms`
    returns it."""
    override = {
        "name": (
            "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
            "collectionGroups/rooms/fields/status"
        ),
        "indexConfig": {
            "indexes": [
                {"order": "ASCENDING",  "queryScope": "COLLECTION",       "state": "READY"},
                {"order": "DESCENDING", "queryScope": "COLLECTION",       "state": "READY"},
                {"arrayConfig": "CONTAINS", "queryScope": "COLLECTION",   "state": "READY"},
                {"order": "ASCENDING",  "queryScope": "COLLECTION_GROUP", "state": "READY"},
            ],
            # Explicit override REPLACES the ancestor default.
            "usesAncestorConfig": False,
        },
    }
    state.setdefault("fields_by_group", {}).setdefault("rooms", []).append(override)


def _apply_deploy_effect(state_path: Path, state: dict) -> None:
    """Mutate the state file so post-deploy fake-gcloud calls
    observe the deploy's effect on production Firestore."""
    effect = state.get("deploy_side_effect", "commit_success")
    if effect == "commit_no_op":
        # Deploy failed OR was a no-op; no production side effect.
        return
    if effect == "commit_success":
        _publish_new_rooms_status_override(state)
        if "poll_state_sequence" not in state:
            state["poll_state_sequence"] = ["CREATING", "READY"]
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_needs_repair":
        _publish_new_rooms_status_override(state)
        state["poll_state_sequence"] = ["CREATING", "NEEDS_REPAIR"]
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_never_ready":
        _publish_new_rooms_status_override(state)
        state["poll_state_sequence"] = ["CREATING"] * 10000
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_leaves_missing":
        # Simulated pathology: the override is registered (visible
        # to `fields list`) but the CG_ASC entry never surfaces in
        # `fields describe`. Polling therefore observes MISSING
        # until the (short in tests) timeout fires.
        _publish_new_rooms_status_override(state)
        state["poll_state_sequence"] = ["MISSING"] * 10000
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_partial_delete_unrelated":
        # Deploy created the intended override AND deleted an
        # unrelated composite index (server-side surprise).
        _publish_new_rooms_status_override(state)
        state["composites"] = []
        state["deploy_committed"] = True
        state["poll_state_sequence"] = ["READY"]
        _save_state(state_path, state)
        return
    if effect == "commit_extra_addition":
        # Deploy created the intended override AND an unrelated
        # composite index that wasn't in PR #42's config.
        _publish_new_rooms_status_override(state)
        extra = {
            "collectionGroup": "unrelated_group",
            "fields": [{"fieldPath": "foo", "order": "ASCENDING"}],
            "queryScope": "COLLECTION",
            "state": "READY",
        }
        state.setdefault("composites", []).append(extra)
        state["deploy_committed"] = True
        state["poll_state_sequence"] = ["READY"]
        _save_state(state_path, state)
        return
    if effect == "sigint_mid_deploy":
        # Send SIGINT to the parent driver process to simulate
        # operator Ctrl-C mid-deploy. The driver's trap must
        # fire. Because the interrupt precedes any real apply,
        # do NOT publish the override — the post-snapshot proves
        # the trap ran even though production state is unchanged.
        state["deploy_committed"] = False
        _save_state(state_path, state)
        parent = os.getppid()
        os.kill(parent, signal.SIGINT)
        time.sleep(0.5)
        return
    # Unknown effect: no-op.


def _handle_deploy(state_path: Path, state: dict) -> int:
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

    if "deploy" in argv:
        return _handle_deploy(state_path, state)

    print(f"fake_firebase: unhandled command {argv!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

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
    - "sigint_mid_deploy_grandchild_ignores_sigterm"  (R5) —
                                     SIGINT parent driver; spawn a
                                     grandchild in the same PG that
                                     installs SIG_IGN for SIGTERM
                                     and would mutate state after
                                     `sabotage_mutation_delay`
                                     seconds. Only a SIGKILL-of-
                                     the-whole-PG (not just wait()
                                     on the direct child) can
                                     prevent the mutation.
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
    # R4 fix: pre-snapshot's rooms.status describe advances the
    # shared poll cursor before deploy, so post-deploy reads would
    # start from index 1 instead of 0 of the intended sequence.
    # Reset the cursor so the sequence is consumed as authored
    # starting from the POST snapshot.
    state["poll_state_cursor"] = 0


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
        # R4 finding 1: fork a child that STAYS in the firebase
        # process group (no setsid). If the driver's trap
        # correctly SIGTERMs the process group AND wait()s, the
        # child is killed before its sleep elapses, and the
        # sabotage-mutation NEVER runs. If the driver only
        # SIGINTs itself without killing the group, the sabotage
        # child completes its sleep and writes the mutation
        # marker.
        ready_path = state_path.parent / "sabotage_child_ready.txt"
        mutation_delay = float(state.get("sabotage_mutation_delay", 2.0))
        pid = os.fork()
        if pid == 0:
            # Child: same process group as firebase (this fake).
            # Write a ready marker BEFORE sleeping so the test can
            # do a readiness handshake — the test waits for this
            # file to appear before signaling the driver.
            try:
                ready_path.write_text(f"{os.getpid()}\n")
            except Exception:
                pass
            # Sleep past the driver's expected termination window.
            # If our PG is SIGTERM'd, this raises and we never
            # reach the mutation. If not killed, we mutate.
            try:
                time.sleep(mutation_delay)
            except Exception:
                os._exit(0)
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
        # Parent (fake firebase): wait for the child's ready
        # marker so the test knows the child exists before we
        # signal the driver. Then SIGINT the driver and hang
        # until the driver's trap terminates our process group.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not ready_path.exists():
            time.sleep(0.05)
        parent_pid = os.getppid()
        os.kill(parent_pid, signal.SIGINT)
        # Hang: the driver's trap should SIGTERM our whole group
        # (us + child) within a few seconds. If we return here
        # normally, the driver has not terminated the group and
        # the sabotage child will complete its mutation.
        time.sleep(max(15.0, mutation_delay + 5.0))
        return
    if effect == "sigint_mid_deploy_grandchild_ignores_sigterm":
        # R5 finding 1: the grandchild STAYS in this process's
        # group AND installs SIG_IGN for SIGTERM. `Popen.wait()`
        # on the direct firebase child (this fake) is not enough
        # to prove quiescence: only a SIGKILL of the entire PG,
        # OR a per-member drain that includes the grandchild,
        # keeps the delayed mutation from running.
        #
        # We schedule the mutation past the driver's SIGTERM
        # grace + SIGKILL grace so the driver MUST reach the
        # SIGKILL path (or wait long enough for a natural exit,
        # which by design never happens within the test window).
        ready_path = state_path.parent / "sabotage_gc_ready.txt"
        mutation_delay = float(state.get("sabotage_mutation_delay", 10.0))
        pid = os.fork()
        if pid == 0:
            # Grandchild: ignore SIGTERM. SIGKILL cannot be caught
            # or ignored, so if the driver escalates it, we die.
            try:
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
            except Exception:
                pass
            try:
                ready_path.write_text(f"{os.getpid()}\n")
            except Exception:
                pass
            # Sleep past the driver's SIGTERM + SIGKILL grace
            # windows so the ONLY way we reach the mutation is if
            # the driver failed to escalate to SIGKILL on the PG.
            #
            # `time.sleep` is signal-interruptible in Python 3.5+,
            # but with SIG_IGN installed, SIGTERM never becomes a
            # deliverable signal for us, so the sleep is NOT
            # interrupted by SIGTERM (only by an uncatchable one,
            # or by natural expiration).
            end = time.monotonic() + mutation_delay
            while time.monotonic() < end:
                time.sleep(0.25)
            try:
                cur = json.loads(state_path.read_text())
                cur["late_child_mutation"] = True
                cur["composites"] = cur.get("composites", []) + [{
                    "collectionGroup": "sabotage_group_gc",
                    "fields": [{"fieldPath": "x", "order": "ASCENDING"}],
                    "queryScope": "COLLECTION",
                    "state": "READY",
                }]
                state_path.write_text(json.dumps(cur, indent=2) + "\n")
            except Exception:
                pass
            os._exit(0)
        # Parent (fake firebase): wait for the grandchild's ready
        # marker so the test knows it's alive before we signal
        # the driver. Then SIGINT the driver and hang for a long
        # time — long enough that a driver that only waits on the
        # DIRECT child (us) but not on the PG would then race the
        # grandchild's mutation.
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not ready_path.exists():
            time.sleep(0.05)
        parent_pid = os.getppid()
        os.kill(parent_pid, signal.SIGINT)
        # Hang: rely on the driver's SIGTERM/SIGKILL to end us.
        time.sleep(max(30.0, mutation_delay + 15.0))
        return
    # R4 finding 4: post-READY late-mutation scenarios. These
    # arm a mutation that fake_gcloud applies AFTER the driver's
    # polling observes READY, so the FINAL snapshot picks up the
    # drift.
    if effect == "commit_success_then_composite_add":
        _publish_rooms_status(state)
        state["poll_state_sequence"] = ["READY"]
        state["pending_late_mutation"] = {
            "add_composite": {
                "name": (
                    "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
                    "collectionGroups/unrelated/indexes/late_composite"
                ),
                "collectionGroup": "unrelated",
                "fields": [{"fieldPath": "foo", "order": "ASCENDING"}],
                "queryScope": "COLLECTION",
                "state": "READY",
            }
        }
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_success_then_unrelated_override_added":
        _publish_rooms_status(state)
        state["poll_state_sequence"] = ["READY"]
        state["pending_late_mutation"] = {
            "add_field_override": {
                "name": (
                    "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
                    "collectionGroups/unrelated/fields/some_field"
                ),
                "indexConfig": {"indexes": [
                    {"fields": [{"fieldPath": "some_field",
                                 "order": "ASCENDING"}],
                     "queryScope": "COLLECTION_GROUP", "state": "READY"},
                ]},
            }
        }
        state["deploy_committed"] = True
        _save_state(state_path, state)
        return
    if effect == "commit_success_then_target_duplicated":
        _publish_rooms_status(state)
        state["poll_state_sequence"] = ["READY"]
        state["pending_late_mutation"] = {
            "duplicate_rooms_status_cg_asc": True,
        }
        state["deploy_committed"] = True
        _save_state(state_path, state)
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

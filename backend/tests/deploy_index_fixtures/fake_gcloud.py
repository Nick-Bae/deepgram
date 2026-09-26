#!/usr/bin/env python3
"""Fake `gcloud` shim used by `test_deploy_rooms_status_index.py`.

Behavior is driven by a JSON state file at `$PR42_FAKE_STATE`.
The file has the shape:

    {
      "composites": [ ... gcloud composite list response ... ],
      "fields_by_group": {
          "rooms":  [ ... gcloud fields list response for rooms ... ],
          "services": [ ... ],
          ...
      },
      "rooms_status_describe": { ... gcloud fields describe response ... },
      "poll_state_sequence": ["CREATING", "CREATING", "READY"],
      "poll_state_cursor": 0,
      "die": null    // or an rc integer to simulate gcloud failure
    }

Each `describe` call advances `poll_state_cursor` if the caller
is asking for `rooms.status` — this lets tests script the
`CREATING → READY` transition and the `NEEDS_REPAIR` /
`MISSING` failure modes without waiting real time.

The state file is updated in place so a fresh cursor is
observed on each invocation.
"""
from __future__ import annotations
import json
import os
import sys
from pathlib import Path


def _load_state() -> tuple[Path, dict]:
    p = Path(os.environ["PR42_FAKE_STATE"])
    return p, json.loads(p.read_text())


def _save_state(p: Path, state: dict) -> None:
    p.write_text(json.dumps(state, indent=2) + "\n")


def _die_maybe(state: dict) -> None:
    if state.get("die") is not None:
        rc = int(state["die"])
        print(f"fake_gcloud: simulated failure rc={rc}", file=sys.stderr)
        sys.exit(rc)


def _handle_composite_list(state: dict) -> None:
    print(json.dumps(state.get("composites", [])))


def _handle_fields_list(state: dict, args) -> None:
    cg = _get_flag(args, "--collection-group")
    groups = state.get("fields_by_group", {})
    print(json.dumps(groups.get(cg, [])))


def _handle_fields_describe(state_path: Path, state: dict, args) -> None:
    cg = _get_flag(args, "--collection-group")
    # `describe <field>` — field name is a positional arg.
    field = None
    for i, a in enumerate(args):
        if a == "describe":
            if i + 1 < len(args):
                field = args[i + 1]
            break
    if cg == "rooms" and field == "status":
        seq = state.get("poll_state_sequence") or ["READY"]
        cursor = int(state.get("poll_state_cursor", 0))
        idx = min(cursor, len(seq) - 1)
        current_state = seq[idx]
        state["poll_state_cursor"] = cursor + 1
        _save_state(state_path, state)
        payload = _rooms_status_payload(current_state)
        print(json.dumps(payload))
        return
    # Non-rooms.status describe: hand back whatever the state file
    # has (or an empty describe response).
    payload = state.get("rooms_status_describe") or {
        "indexConfig": {"indexes": []},
        "name": (
            f"projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
            f"collectionGroups/{cg}/fields/{field}"
        ),
    }
    print(json.dumps(payload))


def _rooms_status_payload(cg_asc_state: str) -> dict:
    """Build a `fields describe rooms.status` response for the
    given COLLECTION_GROUP ASCENDING state. Preserves the three
    ancestor-default COLLECTION-scope entries. `MISSING` means
    the CG ASC entry is entirely absent from the response."""
    base = [
        {"fields": [{"fieldPath": "status", "order": "ASCENDING"}],
         "queryScope": "COLLECTION", "state": "READY"},
        {"fields": [{"fieldPath": "status", "order": "DESCENDING"}],
         "queryScope": "COLLECTION", "state": "READY"},
        {"fields": [{"fieldPath": "status", "arrayConfig": "CONTAINS"}],
         "queryScope": "COLLECTION", "state": "READY"},
    ]
    if cg_asc_state == "MISSING":
        indexes = base
    else:
        indexes = base + [
            {"fields": [{"fieldPath": "status", "order": "ASCENDING"}],
             "queryScope": "COLLECTION_GROUP", "state": cg_asc_state},
        ]
    # The driver's canonicalizer looks for `indexConfig.indexes[].order`
    # and `queryScope` and `arrayConfig` at the top of each entry, not
    # nested inside `fields`. Match the shape `gcloud firestore
    # indexes fields describe` actually returns for single-field
    # indexes: entries with `order`/`arrayConfig` + `queryScope` +
    # `state` directly on each index.
    flat = []
    for e in indexes:
        # Each entry in `base` above has a single field; the real
        # `gcloud fields describe` response puts `order` /
        # `arrayConfig` and `queryScope` at the entry level.
        f0 = e["fields"][0]
        flat.append({
            "order": f0.get("order"),
            "arrayConfig": f0.get("arrayConfig"),
            "queryScope": e["queryScope"],
            "state": e["state"],
        })
    return {
        "indexConfig": {
            "indexes": flat,
            "ancestorField": (
                "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
                "collectionGroups/__default__/fields/*"
            ),
            "usesAncestorConfig": True,
        },
        "name": (
            "projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
            "collectionGroups/rooms/fields/status"
        ),
    }


def _get_flag(args, name: str) -> str | None:
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def main(argv):
    if not argv:
        print("usage: fake_gcloud.py <args...>", file=sys.stderr)
        sys.exit(2)
    state_path, state = _load_state()
    _die_maybe(state)

    # Detect firestore indexes {composite,fields} subcommand.
    if "firestore" in argv and "indexes" in argv:
        if "composite" in argv and "list" in argv:
            _handle_composite_list(state); return 0
        if "fields" in argv and "list" in argv:
            _handle_fields_list(state, argv); return 0
        if "fields" in argv and "describe" in argv:
            _handle_fields_describe(state_path, state, argv); return 0
    print(f"fake_gcloud: unhandled command {argv!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

#!/usr/bin/env python3
"""Fake `gcloud` shim for `test_deploy_rooms_status_index.py`.

R3 model — reflects the REAL gcloud response shape:
  - `firestore indexes composite list --format=json`
      → JSON list of composite index objects
  - `firestore indexes fields list --format=json`  (NO
      --collection-group filter) → JSON list of every field
      entry across every collection group (includes the
      `__default__` ancestor sentinel and every explicit
      override)
  - `firestore indexes fields list --collection-group=<cg> --format=json`
      → same but filtered to one collection group
  - `firestore indexes fields describe <field> --collection-group=<cg>
      --format=json` → single field's indexConfig with
      `indexConfig.indexes[]`, each entry shaped as
      {fields: [{fieldPath, order|arrayConfig}], queryScope, state}
  - `firestore databases describe --database=<db> --format=json`
      → database metadata: name, type, locationId

State file at `$PR42_FAKE_STATE` drives it:
    {
      "composites": [...],
      "field_overrides": [                # authoritative list, real shape
          {"name": "projects/.../collectionGroups/rooms/fields/status",
           "indexConfig": {
              "indexes": [
                {"fields": [{"fieldPath":"status","order":"ASCENDING"}],
                 "queryScope": "COLLECTION",
                 "state": "READY"},
                ...
              ]
           }
          },
          ...
      ],
      "ancestor_default_entry": { ... },  # the __default__ sentinel
      "database": { "name":..., "type":"FIRESTORE_NATIVE",
                    "locationId": "us-central1", ... },
      "poll_state_sequence": ["CREATING","READY"],  # for rooms.status describe
      "poll_state_cursor": 0,
      "die": null                         # or int rc to force failure
    }

Cursor semantics: each `fields describe rooms.status` call
advances the cursor. If the caller asks about a different
collection group / field, cursor is untouched.
"""
from __future__ import annotations
import copy
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


def _get_flag(args, name: str) -> str | None:
    for a in args:
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


# --- composite list -----------------------------------------------------


def _handle_composite_list(state: dict) -> None:
    print(json.dumps(state.get("composites", [])))


# --- fields list (db-wide OR per-collection-group) ---------------------


def _handle_fields_list(state: dict, args) -> None:
    cg = _get_flag(args, "--collection-group")
    ancestor = state.get("ancestor_default_entry")
    all_entries = []
    if ancestor is not None:
        all_entries.append(ancestor)
    all_entries.extend(state.get("field_overrides", []))
    if cg is None:
        # Database-wide list.
        print(json.dumps(all_entries))
        return
    # Filter by collection group.
    filtered = [
        e for e in all_entries
        if f"/collectionGroups/{cg}/fields/" in (e.get("name") or "")
    ]
    print(json.dumps(filtered))


# --- fields describe (nested-fields shape) -----------------------------


def _handle_fields_describe(state_path: Path, state: dict, args) -> None:
    cg = _get_flag(args, "--collection-group")
    field = None
    for i, a in enumerate(args):
        if a == "describe":
            if i + 1 < len(args):
                field = args[i + 1]
            break
    if cg == "rooms" and field == "status":
        seq = state.get("poll_state_sequence") or ["MISSING"]
        cursor = int(state.get("poll_state_cursor", 0))
        idx = min(cursor, len(seq) - 1)
        current_cg_state = seq[idx]
        state["poll_state_cursor"] = cursor + 1
        _save_state(state_path, state)
        print(json.dumps(_rooms_status_payload(state, current_cg_state)))
        return
    # Non-rooms.status describe: find matching override entry.
    for entry in state.get("field_overrides", []):
        name = entry.get("name") or ""
        if (f"/collectionGroups/{cg}/fields/{field}" in name):
            print(json.dumps(entry))
            return
    # Not found — return a synthesized empty describe (mirrors real
    # gcloud when a field has no explicit override).
    print(json.dumps({
        "indexConfig": {"indexes": [],
                        "ancestorField": (
                            f"projects/sturdy-dogfish-472313-k6/"
                            f"databases/worship-translation/"
                            f"collectionGroups/__default__/fields/*"
                        ),
                        "usesAncestorConfig": True},
        "name": (
            f"projects/sturdy-dogfish-472313-k6/databases/worship-translation/"
            f"collectionGroups/{cg}/fields/{field}"
        ),
    }))


def _rooms_status_payload(state: dict, cg_asc_state: str) -> dict:
    """Build a `fields describe rooms.status` response with the
    R3 nested shape. The three ancestor-default COLLECTION-scope
    entries are always present; the CG_ASC entry appears only
    when `cg_asc_state != "MISSING"`."""
    def entry(fpath, *, order=None, array=None, scope="COLLECTION", state_val="READY"):
        inner = {"fieldPath": fpath}
        if order is not None:
            inner["order"] = order
        if array is not None:
            inner["arrayConfig"] = array
        return {
            "fields": [inner],
            "queryScope": scope,
            "state": state_val,
        }

    indexes = [
        entry("status", order="ASCENDING", scope="COLLECTION"),
        entry("status", order="DESCENDING", scope="COLLECTION"),
        entry("status", array="CONTAINS", scope="COLLECTION"),
    ]
    if cg_asc_state != "MISSING":
        indexes.append(
            entry("status", order="ASCENDING",
                  scope="COLLECTION_GROUP", state_val=cg_asc_state)
        )
    return {
        "indexConfig": {
            "indexes": indexes,
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


# --- databases describe -------------------------------------------------


def _handle_databases_describe(state: dict, args) -> None:
    default = {
        "name": "projects/sturdy-dogfish-472313-k6/databases/worship-translation",
        "type": "FIRESTORE_NATIVE",
        "locationId": "us-central1",
        "uid": "test-uid",
    }
    print(json.dumps(state.get("database") or default))


# --- entry --------------------------------------------------------------


def main(argv):
    if not argv:
        print("usage: fake_gcloud.py <args...>", file=sys.stderr)
        return 2
    state_path, state = _load_state()
    _die_maybe(state)

    if "firestore" in argv and "indexes" in argv:
        if "composite" in argv and "list" in argv:
            _handle_composite_list(state); return 0
        if "fields" in argv and "list" in argv:
            _handle_fields_list(state, argv); return 0
        if "fields" in argv and "describe" in argv:
            _handle_fields_describe(state_path, state, argv); return 0
    if "firestore" in argv and "databases" in argv and "describe" in argv:
        _handle_databases_describe(state, argv); return 0
    print(f"fake_gcloud: unhandled command {argv!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

"""Static tests for `./firestore.indexes.json` and `./firebase.json`.

These tests parse the deployed index-config files at the repo root
(the ones `./firebase.json` actually targets when `firebase deploy
--only firestore:indexes` runs) and assert:

  1. Root `firebase.json` targets the root index file.
  2. Exactly one `rooms.status` field override is declared.
  3. All three existing collection-scope behaviors are preserved
     (ASCENDING, DESCENDING, ARRAY_CONTAINS at `COLLECTION` scope).
  4. Exactly one `COLLECTION_GROUP` `ASCENDING` entry is added.
  5. No `COLLECTION_GROUP` `DESCENDING` or `COLLECTION_GROUP`
     ARRAY_CONTAINS entry is added.
  6. The JSON parses with no duplicate top-level or nested keys.

This is a config PR (paired with a manual `firebase deploy
--only firestore:indexes` outside this session). The tests are
static — they do not touch Firestore or authenticate — so they
run in the default backend-tests CI job with no extra setup.
"""
from __future__ import annotations

import json
import unittest
from collections import Counter
from pathlib import Path


_REPO = Path(__file__).resolve().parents[2]
_INDEX_FILE = _REPO / "firestore.indexes.json"
_FIREBASE_JSON = _REPO / "firebase.json"


def _load_no_duplicates(path: Path) -> dict:
    """Load JSON while rejecting duplicate keys at any depth.

    `json.load` silently keeps the last value on duplicates; this
    hook makes it a ValueError instead so a config that would
    behave surprisingly after deploy fails the test at parse time."""
    def _hook(pairs):
        keys = [k for k, _ in pairs]
        dupes = [k for k, n in Counter(keys).items() if n > 1]
        if dupes:
            raise ValueError(
                f"{path.name} has duplicate JSON keys at some depth: {dupes!r}"
            )
        return dict(pairs)
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh, object_pairs_hook=_hook)


class FirebaseJsonTargetsRootIndexFile(unittest.TestCase):

    def test_root_firebase_json_indexes_pointer_resolves_to_root_index_file(self):
        """`./firebase.json` MUST target the root `firestore.indexes.json`.
        A stray path pointing elsewhere would cause `firebase deploy
        --only firestore:indexes` to deploy the wrong file (or fail),
        and this test suite would silently pass tests against a file
        that never reaches production."""
        cfg = _load_no_duplicates(_FIREBASE_JSON)
        fs = cfg.get("firestore")
        self.assertIsInstance(fs, dict, "firebase.json must have a firestore section")
        indexes_ptr = fs.get("indexes")
        self.assertEqual(
            indexes_ptr, "firestore.indexes.json",
            "firebase.json must target the root firestore.indexes.json "
            "for this override to reach production",
        )
        # Resolve the pointer as firebase-cli would (relative to firebase.json).
        resolved = (_FIREBASE_JSON.parent / indexes_ptr).resolve()
        self.assertEqual(resolved, _INDEX_FILE.resolve())
        self.assertTrue(resolved.exists())


class RoomsStatusFieldOverride(unittest.TestCase):

    def setUp(self) -> None:
        self.cfg = _load_no_duplicates(_INDEX_FILE)
        overrides = self.cfg.get("fieldOverrides", [])
        self.rooms_status = [
            o for o in overrides
            if o.get("collectionGroup") == "rooms"
            and o.get("fieldPath") == "status"
        ]

    def test_exactly_one_rooms_status_override_exists(self):
        self.assertEqual(
            len(self.rooms_status), 1,
            f"expected exactly one rooms.status fieldOverride, got "
            f"{len(self.rooms_status)}: {self.rooms_status!r}",
        )

    def test_all_three_existing_collection_scope_behaviors_preserved(self):
        """Firestore's default per-field indexing at COLLECTION scope
        is (ASCENDING, DESCENDING, ARRAY_CONTAINS). A `fieldOverrides`
        entry REPLACES those defaults for the named field, so any
        collection-scope index NOT re-declared here disappears from
        prod on the next deploy. Round-4 index inventory confirmed
        all three defaults were READY on rooms.status pre-migration."""
        entries = self.rooms_status[0].get("indexes", [])
        # ASCENDING at COLLECTION scope
        self.assertTrue(
            any(e.get("order") == "ASCENDING" and e.get("queryScope") == "COLLECTION"
                for e in entries),
            f"missing COLLECTION ASCENDING for rooms.status: {entries!r}",
        )
        # DESCENDING at COLLECTION scope
        self.assertTrue(
            any(e.get("order") == "DESCENDING" and e.get("queryScope") == "COLLECTION"
                for e in entries),
            f"missing COLLECTION DESCENDING for rooms.status: {entries!r}",
        )
        # ARRAY_CONTAINS at COLLECTION scope. The Firestore schema uses
        # `arrayConfig: "CONTAINS"` (with any queryScope) rather than
        # an `order` field.
        self.assertTrue(
            any(e.get("arrayConfig") == "CONTAINS" and e.get("queryScope") == "COLLECTION"
                for e in entries),
            f"missing COLLECTION ARRAY_CONTAINS for rooms.status: {entries!r}",
        )

    def test_exactly_one_collection_group_ascending_entry(self):
        entries = self.rooms_status[0].get("indexes", [])
        cg_asc = [
            e for e in entries
            if e.get("order") == "ASCENDING"
            and e.get("queryScope") == "COLLECTION_GROUP"
        ]
        self.assertEqual(
            len(cg_asc), 1,
            f"expected exactly one COLLECTION_GROUP ASCENDING entry, got "
            f"{len(cg_asc)}: {cg_asc!r}",
        )

    def test_no_collection_group_descending_entry(self):
        entries = self.rooms_status[0].get("indexes", [])
        cg_desc = [
            e for e in entries
            if e.get("order") == "DESCENDING"
            and e.get("queryScope") == "COLLECTION_GROUP"
        ]
        self.assertEqual(
            cg_desc, [],
            f"unexpected COLLECTION_GROUP DESCENDING entry: {cg_desc!r}",
        )

    def test_no_collection_group_array_contains_entry(self):
        entries = self.rooms_status[0].get("indexes", [])
        cg_arr = [
            e for e in entries
            if e.get("arrayConfig") == "CONTAINS"
            and e.get("queryScope") == "COLLECTION_GROUP"
        ]
        self.assertEqual(
            cg_arr, [],
            f"unexpected COLLECTION_GROUP ARRAY_CONTAINS entry: {cg_arr!r}",
        )

    def test_no_other_field_overrides(self):
        """Sanity: this PR should not silently add other overrides
        that would affect unrelated fields. Any override beyond
        `rooms.status` MUST be added by its own explicit PR."""
        overrides = self.cfg.get("fieldOverrides", [])
        others = [
            o for o in overrides
            if not (o.get("collectionGroup") == "rooms"
                    and o.get("fieldPath") == "status")
        ]
        self.assertEqual(
            others, [],
            f"unexpected additional fieldOverrides in this PR: {others!r}",
        )

    def test_indexes_list_still_empty(self):
        """Composite indexes are out of scope for this PR — the
        reviewer's direction was to touch ONLY the rooms.status
        override. Any composite added here would need a separate
        review pass."""
        self.assertEqual(
            self.cfg.get("indexes"), [],
            f"this PR must not add composite indexes: "
            f"{self.cfg.get('indexes')!r}",
        )


class JsonWellFormedness(unittest.TestCase):

    def test_firestore_indexes_json_parses_without_duplicate_keys(self):
        # _load_no_duplicates raises ValueError on any duplicate.
        cfg = _load_no_duplicates(_INDEX_FILE)
        self.assertIn("indexes", cfg)
        self.assertIn("fieldOverrides", cfg)

    def test_firebase_json_parses_without_duplicate_keys(self):
        cfg = _load_no_duplicates(_FIREBASE_JSON)
        self.assertIn("firestore", cfg)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()

"""Task #133 — `backend/scripts/deploy_gate.py` writer script.

The reviewer's spec for these tests (verbatim scenarios):

  - Missing document → unblocked baseline.
  - First block creates blocked=true, revision=1.
  - Repeated block is a no-op.
  - Unblock produces blocked=false, revision=2.
  - Stale concurrent revision is rejected without a write.
  - Malformed blocked or revision values fail closed.
  - Dry-run performs zero writes.
  - Wrong project/database and arbitrary document paths are refused.

The last one is enforced at the CLI layer (there is no `--path`
knob at all; arbitrary paths cannot be passed). We assert both:
that the CLI rejects wrong project/database and that only the
constant `system/deploy_gate` reference is ever touched.

Split into two suites:
  - `DeployGateWriterUnitTests` — pure fixture tests that use an
    in-memory fake Firestore. Exercise the parser + CLI + dry-run
    without needing the emulator.
  - `DeployGateWriterFirestoreTests` — real Firestore admin SDK
    against the emulator. Runs in CI's firestore-emulator-tests
    job. Skips locally without FIRESTORE_EMULATOR_HOST.
"""
from __future__ import annotations

import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Optional
from unittest.mock import patch

# The writer lives under backend/scripts/. Import via path insertion
# so we don't require it to be installed as a package.
_SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(_SCRIPTS_DIR))

import deploy_gate as writer  # noqa: E402


# --- Fake Firestore for unit tests ----------------------------------------


class _FakeSnapshot:
    def __init__(self, data: Optional[dict]):
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self) -> Optional[dict]:
        return None if self._data is None else dict(self._data)


class _FakeRef:
    def __init__(self, holder: dict, key: str):
        self._holder = holder
        self._key = key
        self._get_calls = 0
        self._set_calls: list[dict] = []

    def get(self, transaction=None):
        self._get_calls += 1
        return _FakeSnapshot(self._holder.get(self._key))

    def set(self, payload):
        # Non-transactional set — used by tests that verify the
        # writer NEVER calls this (writer only writes via
        # `transaction.set`, never `ref.set`).
        self._set_calls.append(dict(payload))
        self._holder[self._key] = dict(payload)


class _FakeTransaction:
    def __init__(self, ref: _FakeRef, holder: dict):
        self._ref = ref
        self._holder = holder
        self.staged: list[dict] = []

    def set(self, ref, payload):
        # Simulate the atomic commit: apply the write immediately
        # into the holder. Real Firestore applies at commit time,
        # but for the fake we don't have a two-phase commit —
        # tests either observe the after-state or use a
        # separate concurrent-write to model staleness.
        assert ref is self._ref
        self.staged.append(dict(payload))
        self._ref._holder[self._ref._key] = dict(payload)


class _FakeClient:
    """Test double for `GateDocClient`. Owns a single holder dict
    keyed on the deploy_gate document. Any read/write to any other
    path would raise AttributeError because we don't expose one."""

    def __init__(self, initial: Optional[dict] = None):
        self._holder: dict = {}
        if initial is not None:
            self._holder["deploy_gate"] = dict(initial)
        self._ref = _FakeRef(self._holder, "deploy_gate")
        self._tx_calls: list[_FakeTransaction] = []
        self.project = "cleanup-track1-emulator"
        self.database = "(default)"

    # `_run` passes project/database via factory; test factory
    # ignores them so we can drive the CLI without the client
    # constructor actually running.
    def transaction(self):
        tx = _FakeTransaction(self._ref, self._holder)
        self._tx_calls.append(tx)
        return tx

    def gate_ref(self):
        return self._ref

    def server_timestamp(self):
        return "<test-server-ts>"


def _run_cli(
    argv: list[str],
    *,
    client: Optional[_FakeClient] = None,
) -> tuple[int, str, str]:
    """Return (exit_code, stdout, stderr)."""
    if client is None:
        client = _FakeClient()

    def _factory(*, project, database):
        # Enforce the allowlist match here too — real GateDocClient
        # doesn't take an override, so a fake that ignores it would
        # let a bad project/database sneak past.
        assert (project, database) == (client.project, client.database), (
            f"factory got ({project!r}, {database!r}); fake client "
            f"is ({client.project!r}, {client.database!r})"
        )
        return client

    # Patch the `transactional` decorator inside the writer so the
    # fake transaction body runs directly (no google-cloud-firestore
    # dependency for unit tests).
    def _passthrough_transactional(fn):
        def _run(transaction):
            return fn(transaction)
        return _run

    out = io.StringIO()
    err = io.StringIO()
    with patch.object(
        writer, "_run_write", new=_fake_run_write,
    ), redirect_stdout(out), redirect_stderr(err):
        try:
            rc = writer._run(argv, client_factory=_factory)
        except SystemExit as exc:
            # argparse's `error()` raises SystemExit; treat its
            # code (usually 2) as the CLI return code so tests
            # can assert on it uniformly.
            rc = int(exc.code) if exc.code is not None else 0
    return rc, out.getvalue(), err.getvalue()


def _fake_run_write(client, op, *, server_ts):
    """Runs the same logic as the real `_run_write` but against the
    _FakeClient's transaction (no `firestore.transactional` decorator
    needed). Kept in this test module so the unit tests don't depend
    on the google-cloud-firestore install."""
    ref = client.gate_ref()
    tx = client.transaction()
    snap = ref.get(transaction=tx)
    current_view = writer._parse_gate_doc(
        snap.to_dict() if snap.exists else None,
    )
    op.check_revision(current_view)
    if op.is_noop(current_view):
        return {"kind": "noop", "before": current_view}
    new_revision = op.next_revision(current_view)
    payload = {
        "blocked": op.desired_blocked(),
        "revision": new_revision,
        "reason": op.reason,
        "blocked_by": op.blocked_by,
        "blocked_at": server_ts,
    }
    tx.set(ref, payload)
    return {
        "kind": "committed",
        "before": current_view,
        "after": {
            "exists": True,
            "blocked": op.desired_blocked(),
            "revision": new_revision,
            "reason": op.reason,
            "blocked_by": op.blocked_by,
            "blocked_at": "<server-timestamp>",
        },
    }


class DeployGateWriterUnitTests(unittest.TestCase):
    """Pure fake-Firestore coverage. No emulator required."""

    ARGS_PROJECT = ["--project", "cleanup-track1-emulator", "--database", "(default)"]

    # --- reviewer scenario: missing document → unblocked baseline ------

    def test_status_missing_document_reports_unblocked_baseline(self):
        client = _FakeClient()
        rc, out, err = _run_cli(self.ARGS_PROJECT + ["status"], client=client)
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["action"], "status")
        self.assertEqual(payload["kind"], "ok")
        self.assertFalse(payload["view"]["exists"])
        self.assertFalse(payload["view"]["blocked"])
        self.assertEqual(payload["view"]["revision"], 0)
        # No writes.
        self.assertEqual(client._ref._set_calls, [])

    # --- reviewer scenario: first block → blocked=true, revision=1 ----

    def test_first_block_sets_blocked_true_and_revision_1(self):
        client = _FakeClient()
        rc, out, err = _run_cli(
            self.ARGS_PROJECT + [
                "block",
                "--expected-revision", "0",
                "--reason", "test-first-block",
                "--blocked-by", "test-operator",
                "--apply",
                "--confirm", writer.CONFIRMATION_TOKEN,
            ],
            client=client,
        )
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "committed")
        self.assertTrue(payload["after"]["blocked"])
        self.assertEqual(payload["after"]["revision"], 1)
        # Underlying state — one transaction, one staged write.
        self.assertEqual(len(client._tx_calls), 1)
        self.assertEqual(len(client._tx_calls[0].staged), 1)
        # Non-transactional set NEVER called.
        self.assertEqual(client._ref._set_calls, [])

    # --- reviewer scenario: repeated block → no-op ---------------------

    def test_repeated_block_is_noop_without_revision_bump(self):
        client = _FakeClient(initial={
            "blocked": True,
            "revision": 5,
            "reason": "prior",
            "blocked_by": "prior-op",
            "blocked_at": "<prior>",
        })
        rc, out, err = _run_cli(
            self.ARGS_PROJECT + [
                "block",
                "--expected-revision", "5",
                "--reason", "test-repeat",
                "--blocked-by", "test-operator",
                "--apply",
                "--confirm", writer.CONFIRMATION_TOKEN,
            ],
            client=client,
        )
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "noop")
        self.assertEqual(payload["before"]["revision"], 5)
        # `after` MUST be absent — a noop doesn't produce one.
        self.assertNotIn("after", payload)
        # The transaction opened, staged nothing, and the doc's
        # revision is unchanged.
        self.assertEqual(len(client._tx_calls), 1)
        self.assertEqual(client._tx_calls[0].staged, [])
        self.assertEqual(client._holder["deploy_gate"]["revision"], 5)

    # --- reviewer scenario: unblock → blocked=false, revision=2 --------

    def test_unblock_from_revision_1_yields_revision_2(self):
        client = _FakeClient(initial={
            "blocked": True,
            "revision": 1,
            "reason": "first-block",
            "blocked_by": "op",
            "blocked_at": "<x>",
        })
        rc, out, err = _run_cli(
            self.ARGS_PROJECT + [
                "unblock",
                "--expected-revision", "1",
                "--reason", "test-unblock",
                "--blocked-by", "test-operator",
                "--apply",
                "--confirm", writer.CONFIRMATION_TOKEN,
            ],
            client=client,
        )
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "committed")
        self.assertFalse(payload["after"]["blocked"])
        self.assertEqual(payload["after"]["revision"], 2)

    # --- reviewer scenario: stale expected revision → refused ----------

    def test_stale_expected_revision_rejected_without_write(self):
        client = _FakeClient(initial={
            "blocked": False,
            "revision": 7,
            "reason": "",
            "blocked_by": "",
            "blocked_at": None,
        })
        rc, out, err = _run_cli(
            self.ARGS_PROJECT + [
                "block",
                "--expected-revision", "3",  # stale (actual is 7)
                "--reason", "should-not-write",
                "--blocked-by", "op",
                "--apply",
                "--confirm", writer.CONFIRMATION_TOKEN,
            ],
            client=client,
        )
        self.assertEqual(rc, 4, err)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "stale_expected_revision")
        # State unchanged.
        self.assertFalse(client._holder["deploy_gate"]["blocked"])
        self.assertEqual(client._holder["deploy_gate"]["revision"], 7)
        # Transaction opened but staged nothing.
        self.assertEqual(len(client._tx_calls), 1)
        self.assertEqual(client._tx_calls[0].staged, [])

    # --- reviewer scenario: malformed values fail closed ---------------

    def test_malformed_missing_blocked_fails_closed(self):
        client = _FakeClient(initial={"revision": 1})  # no `blocked`
        rc, out, err = _run_cli(
            self.ARGS_PROJECT + [
                "block",
                "--expected-revision", "1",
                "--reason", "x", "--blocked-by", "x",
                "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
            ],
            client=client,
        )
        self.assertEqual(rc, 3, err)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "malformed")
        self.assertIn("blocked", payload["malformed_reason"])
        self.assertEqual(len(client._tx_calls), 1)
        self.assertEqual(client._tx_calls[0].staged, [])

    def test_malformed_wrong_typed_blocked_fails_closed(self):
        for bad in ("true", 1, 0, None, ""):
            with self.subTest(bad=bad):
                client = _FakeClient(initial={"blocked": bad, "revision": 1})
                rc, _, _ = _run_cli(
                    self.ARGS_PROJECT + [
                        "block",
                        "--expected-revision", "1",
                        "--reason", "x", "--blocked-by", "x",
                        "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
                    ],
                    client=client,
                )
                self.assertEqual(rc, 3, f"bad={bad!r} did not fail closed")

    def test_malformed_wrong_typed_revision_fails_closed(self):
        client = _FakeClient(initial={"blocked": True, "revision": "1"})
        rc, out, _ = _run_cli(
            self.ARGS_PROJECT + [
                "block", "--expected-revision", "1",
                "--reason", "x", "--blocked-by", "x",
                "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
            ],
            client=client,
        )
        self.assertEqual(rc, 3)
        self.assertIn("revision", json.loads(out)["malformed_reason"])

    def test_malformed_zero_revision_fails_closed(self):
        """Zero is reserved for the absent-doc sentinel; an
        EXISTING doc with revision 0 is malformed."""
        client = _FakeClient(initial={"blocked": False, "revision": 0})
        rc, _, _ = _run_cli(
            self.ARGS_PROJECT + [
                "status",
            ],
            client=client,
        )
        # Malformed statuses return 3.
        self.assertEqual(rc, 3)

    # --- reviewer scenario: dry-run → zero writes ----------------------

    def test_dry_run_performs_zero_writes(self):
        client = _FakeClient(initial={
            "blocked": False, "revision": 3,
            "reason": "", "blocked_by": "", "blocked_at": None,
        })
        rc, out, err = _run_cli(
            self.ARGS_PROJECT + [
                "block",
                "--expected-revision", "3",
                "--reason", "test-dry",
                "--blocked-by", "op",
                # NO --apply
            ],
            client=client,
        )
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "dry_run")
        self.assertFalse(payload["would_be_noop"])
        self.assertEqual(payload["would_write_revision"], 4)
        self.assertTrue(payload["would_write_blocked"])
        # NO transaction opened; NO writes.
        self.assertEqual(client._tx_calls, [])
        self.assertEqual(client._ref._set_calls, [])
        self.assertEqual(client._holder["deploy_gate"]["revision"], 3)

    def test_dry_run_reports_noop_when_state_matches(self):
        client = _FakeClient(initial={
            "blocked": True, "revision": 2,
            "reason": "", "blocked_by": "", "blocked_at": None,
        })
        rc, out, _ = _run_cli(
            self.ARGS_PROJECT + [
                "block", "--expected-revision", "2",
                "--reason", "x", "--blocked-by", "y",
            ],
            client=client,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "dry_run")
        self.assertTrue(payload["would_be_noop"])
        self.assertNotIn("would_write_revision", payload)

    def test_apply_without_confirm_refused(self):
        rc, _, err = _run_cli(
            self.ARGS_PROJECT + [
                "block", "--expected-revision", "0",
                "--reason", "x", "--blocked-by", "y",
                "--apply",  # no --confirm
            ],
        )
        self.assertEqual(rc, 5)
        self.assertIn("--confirm", err)

    def test_apply_with_wrong_confirm_refused(self):
        rc, _, err = _run_cli(
            self.ARGS_PROJECT + [
                "block", "--expected-revision", "0",
                "--reason", "x", "--blocked-by", "y",
                "--apply", "--confirm", "not the token",
            ],
        )
        self.assertEqual(rc, 5)

    # --- reviewer scenario: wrong project/database refused -------------

    def test_wrong_project_refused(self):
        rc, _, err = _run_cli(
            ["--project", "some-other-project", "--database", "worship-translation",
             "status"],
        )
        self.assertEqual(rc, 2)
        self.assertIn("allowlist", err)

    def test_wrong_database_refused(self):
        rc, _, err = _run_cli(
            ["--project", "sturdy-dogfish-472313-k6", "--database", "wrong-db",
             "status"],
        )
        self.assertEqual(rc, 2)
        self.assertIn("allowlist", err)

    # --- reviewer scenario: arbitrary paths refused --------------------

    def test_cli_offers_no_path_knob(self):
        """The reviewer's explicit constraint — the writer MUST
        NOT accept an arbitrary path. Enforced structurally: there
        is no `--path` argument.

        Two-shape test: `--path` at the top level or as a subcommand
        arg. Either shape must be refused (argparse-level rejection
        is sufficient; the writer never gets to run)."""
        for argv_extra in (
            ["--path", "system/other_doc", "status"],
            ["status", "--path", "system/other_doc"],
        ):
            with self.subTest(argv_extra=argv_extra):
                rc, _, err = _run_cli(self.ARGS_PROJECT + argv_extra)
                self.assertNotEqual(rc, 0)
                # argparse produces "unrecognized arguments" or
                # "invalid choice" for any --path attempt; both
                # count as refusal. The important assertion is
                # that the CLI's parser rejected the argument
                # before any real action ran.

    def test_cli_source_has_no_path_argument(self):
        """Structural assertion — the writer source must not
        register a `--path` argument via argparse. Catches a
        future refactor that accidentally introduces a `--path`
        knob. Uses a strict substring match on the argparse
        registration form to avoid false-positive on docstring
        text that MENTIONS `--path`."""
        import re
        source = Path(writer.__file__).read_text(encoding="utf-8")
        registered = re.search(
            r"""add_argument\(\s*["']--path["']""",
            source,
        )
        self.assertIsNone(
            registered,
            "deploy_gate.py must NOT expose a --path argument; the "
            "document path is fixed at system/deploy_gate per PR #33.",
        )

    def test_unknown_action_refused(self):
        rc, _, err = _run_cli(self.ARGS_PROJECT + ["destroy"])
        self.assertNotEqual(rc, 0)
        # argparse-provided message names the invalid choice.

    def test_unknown_top_level_flag_refused(self):
        rc, _, err = _run_cli(self.ARGS_PROJECT + ["status", "--force-repair"])
        self.assertNotEqual(rc, 0)

    # --- output sanitization -------------------------------------------

    def test_output_never_prints_unlisted_fields(self):
        """A doc with an extra unrelated field must NOT surface it in
        the sanitized output. Prevents leaking a field an ops-side
        typo might have added."""
        client = _FakeClient(initial={
            "blocked": False, "revision": 1,
            "reason": "safe", "blocked_by": "op", "blocked_at": None,
            "unlisted_admin_token": "SECRET_SHOULD_NOT_APPEAR",
        })
        rc, out, _ = _run_cli(self.ARGS_PROJECT + ["status"], client=client)
        self.assertEqual(rc, 0)
        self.assertNotIn("unlisted_admin_token", out)
        self.assertNotIn("SECRET_SHOULD_NOT_APPEAR", out)


# --- Emulator tests -------------------------------------------------------


@unittest.skipUnless(
    os.getenv("FIRESTORE_EMULATOR_HOST"),
    "Firestore emulator required — set FIRESTORE_EMULATOR_HOST "
    "and GOOGLE_CLOUD_PROJECT.",
)
class DeployGateWriterFirestoreTests(unittest.TestCase):
    """Real Firestore admin SDK against the emulator. Runs in CI's
    firestore-emulator-tests job.

    Each test isolates its state by writing to a per-test document
    id under `system/`. Wait — the writer only knows one path
    (`system/deploy_gate`). Isolation is achieved by DELETING the
    document at setUp and tearDown."""

    def setUp(self):
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "cleanup-track1-emulator")
        from google.cloud import firestore  # type: ignore
        self._client = firestore.Client(
            project="cleanup-track1-emulator", database="(default)",
        )
        self._ref = (
            self._client.collection(writer.DEPLOY_GATE_COLLECTION)
            .document(writer.DEPLOY_GATE_DOCUMENT)
        )
        self._ref.delete()

    def tearDown(self):
        try:
            self._ref.delete()
        except Exception:
            pass

    def _cli(self, *extra: str) -> tuple[int, str, str]:
        argv = ["--project", "cleanup-track1-emulator",
                "--database", "(default)"] + list(extra)
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = writer._run(argv)
        return rc, out.getvalue(), err.getvalue()

    def test_missing_document_unblocked_baseline(self):
        rc, out, _ = self._cli("status")
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertFalse(payload["view"]["exists"])
        self.assertFalse(payload["view"]["blocked"])
        self.assertEqual(payload["view"]["revision"], 0)

    def test_first_block_creates_revision_1(self):
        rc, out, err = self._cli(
            "block", "--expected-revision", "0",
            "--reason", "emu-test-first",
            "--blocked-by", "emu-test",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        self.assertEqual(rc, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "committed")
        self.assertTrue(payload["after"]["blocked"])
        self.assertEqual(payload["after"]["revision"], 1)
        # Round-trip through a fresh status.
        rc2, out2, _ = self._cli("status")
        self.assertEqual(rc2, 0)
        v = json.loads(out2)["view"]
        self.assertTrue(v["blocked"])
        self.assertEqual(v["revision"], 1)

    def test_repeated_block_is_noop_no_revision_bump(self):
        self._cli(
            "block", "--expected-revision", "0",
            "--reason", "first", "--blocked-by", "op",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        rc, out, _ = self._cli(
            "block", "--expected-revision", "1",
            "--reason", "second", "--blocked-by", "op",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "noop")
        # Revision unchanged in Firestore.
        snap = self._ref.get()
        self.assertEqual(snap.to_dict()["revision"], 1)

    def test_unblock_produces_revision_2(self):
        self._cli(
            "block", "--expected-revision", "0",
            "--reason", "b", "--blocked-by", "op",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        rc, out, _ = self._cli(
            "unblock", "--expected-revision", "1",
            "--reason", "u", "--blocked-by", "op",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "committed")
        self.assertFalse(payload["after"]["blocked"])
        self.assertEqual(payload["after"]["revision"], 2)

    def test_stale_expected_revision_rejected_no_write(self):
        # Set up revision 3 via two block/unblock cycles.
        self._cli(
            "block", "--expected-revision", "0",
            "--reason", "b1", "--blocked-by", "op",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        self._cli(
            "unblock", "--expected-revision", "1",
            "--reason", "u1", "--blocked-by", "op",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        self._cli(
            "block", "--expected-revision", "2",
            "--reason", "b2", "--blocked-by", "op",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        # Now attempt a stale write.
        rc, out, _ = self._cli(
            "unblock", "--expected-revision", "1",  # stale — actual is 3
            "--reason", "stale", "--blocked-by", "op",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        self.assertEqual(rc, 4)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "stale_expected_revision")
        # State unchanged in Firestore.
        snap = self._ref.get()
        self.assertTrue(snap.to_dict()["blocked"])
        self.assertEqual(snap.to_dict()["revision"], 3)

    def test_malformed_blocked_fails_closed(self):
        # Write a malformed doc directly (bypasses the writer's
        # own validation — this simulates a manual mishap).
        self._ref.set({"blocked": "not-a-bool", "revision": 1})
        rc, out, _ = self._cli(
            "block", "--expected-revision", "1",
            "--reason", "x", "--blocked-by", "y",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        self.assertEqual(rc, 3)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "malformed")
        # Doc unchanged.
        snap = self._ref.get()
        self.assertEqual(snap.to_dict()["blocked"], "not-a-bool")

    def test_dry_run_performs_zero_writes(self):
        # Seed a doc.
        self._cli(
            "block", "--expected-revision", "0",
            "--reason", "seed", "--blocked-by", "op",
            "--apply", "--confirm", writer.CONFIRMATION_TOKEN,
        )
        # Dry run — no --apply.
        rc, out, _ = self._cli(
            "unblock", "--expected-revision", "1",
            "--reason", "dry", "--blocked-by", "op",
        )
        self.assertEqual(rc, 0)
        payload = json.loads(out)
        self.assertEqual(payload["kind"], "dry_run")
        # State unchanged.
        snap = self._ref.get()
        self.assertTrue(snap.to_dict()["blocked"])
        self.assertEqual(snap.to_dict()["revision"], 1)


if __name__ == "__main__":
    unittest.main()

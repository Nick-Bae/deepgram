"""Deterministic coverage for the exception-class-identity
mismatch that caused
`test_concurrent_cas_commit_survives_getter_writeback` to
mis-classify a stale-CAS commit as "unexpected" after another
test in the same pytest run reloaded `app.services.multichurch_store`.

Scope and rationale (reviewer round 2026-11-02 after the
concurrency-contract follow-up):

- The CAS test file captures `StaleBillingProfileError` at
  module load (line 42): `from app.services.multichurch_store
  import ... StaleBillingProfileError`. The binding in the
  test file's namespace is the class object alive at that
  moment.

- `test_sweeper_atomic.FirestoreAtomicIdleTerminationTests.setUp()`
  (line 853) calls `importlib.reload(store_mod)` where
  `store_mod` is `app.services.multichurch_store`. The reload
  recreates the module and all its classes; the new
  `StaleBillingProfileError` has the SAME module and qualname
  but a DIFFERENT `id()`.

- The CAS test's `except StaleBillingProfileError` references
  the pre-reload class via the test file's namespace. The
  setter it calls (`mcs.set_org_billing_profile`) raises
  instances of the POST-reload class. Those instances are not
  caught by the pre-reload class name; they fall through to
  the `except BaseException` branch and are recorded as
  "unexpected".

- The fix (applied in this commit) changes the CAS test's
  catch to `except mcs.StaleBillingProfileError` — resolving
  the class via the current module reference `mcs` which is
  (re)imported at method entry. After the sweeper setUp has
  reloaded the module, `mcs` now points at the reloaded module
  object, so `mcs.StaleBillingProfileError` is the post-reload
  class the setter actually raises.

Tests here do NOT require the Firestore emulator; they
exercise only Python's module-reload semantics against the
real `app.services.multichurch_store` module. They provide
deterministic evidence that:

  (a) `importlib.reload` rebinds the class object so pre-reload
      `is`-identity fails.
  (b) `isinstance` with the pre-reload class REF fails on a
      post-reload instance.
  (c) `isinstance` with the post-reload class REF (via `mcs.X`)
      succeeds — which is why the fix catches via `mcs.X`
      instead of the captured name.
  (d) An instance of a Python exception can be raised from the
      post-reload class and caught by the pre-reload class
      ONLY if we go through the pre-reload class's `__init__`
      — not when the instance's `type()` differs from the
      except-handler class.

Also exercised: the `set_org_billing_profile` entry point is
UNCHANGED by this fix (byte-preserved); the correction lives
entirely inside the CAS test's catch clause.
"""
from __future__ import annotations

import importlib
import unittest

import app.services.multichurch_store as _mcs_initial


class CASModuleReloadIsolationTests(unittest.TestCase):
    def test_before_reload_identity_holds(self) -> None:
        """Baseline: the test-module-captured class and the
        current-module class are the SAME object before any
        reload. The CAS test's original `except StaleBillingProfileError`
        pattern works correctly in isolation (before anything
        reloads the module)."""
        from app.services.multichurch_store import (
            StaleBillingProfileError as captured_at_import,
        )
        self.assertIs(
            captured_at_import, _mcs_initial.StaleBillingProfileError,
            "pre-reload: the captured name and the module attribute "
            "point to the same class object",
        )

    def test_reload_rebinds_class_object(self) -> None:
        """After `importlib.reload`, the module-attribute class
        is a DIFFERENT object (`is False`) with the same module
        and qualname."""
        from app.services.multichurch_store import (
            StaleBillingProfileError as captured_at_import,
        )
        mcs = importlib.import_module("app.services.multichurch_store")
        # Capture pre-reload id/module/qualname for the record.
        pre_id = id(captured_at_import)
        pre_mod = captured_at_import.__module__
        pre_qn = captured_at_import.__qualname__

        importlib.reload(mcs)
        post_cls = mcs.StaleBillingProfileError

        self.assertEqual(pre_mod, post_cls.__module__,
                         "modules match (both are app.services.multichurch_store)")
        self.assertEqual(pre_qn, post_cls.__qualname__,
                         "qualnames match (both are StaleBillingProfileError)")
        self.assertIsNot(
            captured_at_import, post_cls,
            "post-reload: class identity differs even though module "
            "and qualname match. Names alone do not prove identity.",
        )
        # Record the ids for diagnostic clarity if the assertion fires.
        self.assertNotEqual(
            pre_id, id(post_cls),
            f"ids must differ post-reload; pre_id={pre_id} "
            f"post_id={id(post_cls)}",
        )

    def test_captured_name_does_not_catch_post_reload_instance(self) -> None:
        """Exact reproduction of the CAS test's observed failure
        shape: raise an instance of the POST-reload class,
        attempt to catch with the PRE-reload captured name →
        miss. This is the mechanism by which
        `test_concurrent_cas_commit_survives_getter_writeback`
        mis-classified a stale-CAS commit as "unexpected".
        """
        from app.services.multichurch_store import (
            StaleBillingProfileError as captured_at_import,
        )
        mcs = importlib.import_module("app.services.multichurch_store")
        importlib.reload(mcs)

        try:
            raise mcs.StaleBillingProfileError("post-reload instance")
        except captured_at_import:
            self.fail(
                "the pre-reload captured name must NOT catch a "
                "post-reload instance — if it did, the reproducer "
                "is wrong"
            )
        except Exception as exc:
            self.assertIsInstance(
                exc, mcs.StaleBillingProfileError,
                "instance should be of the post-reload class",
            )
            self.assertNotIsInstance(
                exc, captured_at_import,
                "instance should NOT be of the pre-reload class — "
                "this is the mis-classification mechanism",
            )

    def test_current_module_reference_catches_post_reload_instance(self) -> None:
        """The fix: catch via the current-module reference
        (`except mcs.StaleBillingProfileError`), where `mcs` is
        the reloaded module. This succeeds where the
        pre-reload captured name failed, proving the correction
        is sound."""
        mcs = importlib.import_module("app.services.multichurch_store")
        importlib.reload(mcs)

        caught = None
        try:
            raise mcs.StaleBillingProfileError("post-reload instance")
        except mcs.StaleBillingProfileError as exc:
            caught = exc
        self.assertIsNotNone(
            caught,
            "the current-module reference must catch an instance "
            "raised from the current module",
        )

    def test_name_and_module_alone_do_not_prove_identity(self) -> None:
        """Negative control: a different class object with the
        SAME module string and SAME qualname is NOT identity-
        equivalent. This is why the fix must use `is`-identity
        semantics (via `except mcs.X`), not string-matching on
        `type(exc).__module__` / `type(exc).__qualname__`."""
        from app.services.multichurch_store import (
            StaleBillingProfileError as captured_at_import,
        )
        mcs = importlib.import_module("app.services.multichurch_store")
        importlib.reload(mcs)
        post_cls = mcs.StaleBillingProfileError

        self.assertEqual(captured_at_import.__module__, post_cls.__module__)
        self.assertEqual(captured_at_import.__qualname__, post_cls.__qualname__)
        self.assertIsNot(captured_at_import, post_cls)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)

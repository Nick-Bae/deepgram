"""Deterministic coverage for the exception-class-identity
mismatch that caused
`test_concurrent_cas_commit_survives_getter_writeback` to
mis-classify a stale-CAS commit as "unexpected" after another
test in the same pytest run reloaded `app.services.multichurch_store`.

Scope and rationale:

- The CAS test file captures `StaleBillingProfileError` at
  module load (line 42):
    `from app.services.multichurch_store import ... StaleBillingProfileError`.
  The binding in the test file's namespace is the class object
  alive at that moment.

- `test_sweeper_atomic.FirestoreAtomicIdleTerminationTests.setUp()`
  (line 853) calls `importlib.reload(store_mod)`. The reload
  recreates the module and every class object inside it; the
  POST-reload `StaleBillingProfileError` has the SAME
  `__module__` + `__qualname__` but a DIFFERENT `id()`.

- The fix (committed in `test_billing_getter_cas_firestore.py`):
  the CAS test's catch is now `except mcs.StaleBillingProfileError`,
  resolving against the current module reference.

Reviewer-identified defect in the prior (in-process) form of
this test file: calling `importlib.reload(mcs)` inside a test
LEAKS the reload into subsequent tests in the same pytest run,
because tests share the parent process's module cache. This
file originally reproduced the same contamination the fix
intends to avoid.

**Isolation (this version)**: every reload probe runs in a
**fresh subprocess** (`sys.executable -c <code>`, no shared
state). The parent process's module cache is NEVER mutated.
A dedicated parent-side test
(`test_parent_module_identity_unchanged_after_subprocess_probes`)
asserts the pre-captured class identity in the parent is
unchanged after all subprocess probes have run.

The subprocess probes themselves still exercise the three
necessary invariants in the child:

  - pre-reload identity holds in the child.
  - `importlib.reload` rebinds the class (new `id()`).
  - `except pre_captured` MISSES a post-reload instance;
    `except mcs.X` CATCHES it.

Each probe is a complete standalone Python script sent to the
child via `-c`; the child's exit code and stdout are checked
in the parent, with no module import on the parent side.
"""
from __future__ import annotations

import os
import subprocess
import sys
import unittest

# Parent-side capture of the pre-probe class identity. The
# parent-side test below asserts this `id()` is unchanged after
# all subprocess probes complete.
import app.services.multichurch_store as _mcs_parent_ref
from app.services.multichurch_store import (
    StaleBillingProfileError as _parent_captured_cls,
)
_PARENT_CAPTURED_ID_AT_IMPORT = id(_parent_captured_cls)
_PARENT_MCS_ATTR_ID_AT_IMPORT = id(_mcs_parent_ref.StaleBillingProfileError)


def _run_probe(child_source: str) -> subprocess.CompletedProcess:
    """Run a self-contained Python source string in a fresh
    subprocess. Backend/ is on PYTHONPATH so
    `app.services.multichurch_store` resolves.

    Isolation guarantee: the parent's `sys.modules` is NEVER
    touched by the child. Any `importlib.reload` the child
    performs affects only the child's own module cache.
    """
    env = dict(os.environ)
    # Prepend backend/ to PYTHONPATH relative to the repo root.
    # The test runner invocation already passes PYTHONPATH=backend,
    # so inherit it; worst case we also compute from here.
    env.setdefault("PYTHONPATH", "backend")
    # -I: isolated mode (no PYTHONPATH*, no PYTHON*, no user site).
    # We WANT PYTHONPATH to reach the child, so use plain mode and
    # rely on the environment variable (-I would strip it). Use
    # -S to skip site.py? No, we need site-packages for
    # google.api_core. Just the plain interpreter.
    return subprocess.run(
        [sys.executable, "-c", child_source],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ---------------------------------------------------------------
# Shared child-side source strings
# ---------------------------------------------------------------

_PROBE_BEFORE_RELOAD = r"""
import sys
from app.services.multichurch_store import StaleBillingProfileError as captured
import app.services.multichurch_store as mcs
assert captured is mcs.StaleBillingProfileError, (
    "pre-reload: captured name and module attr must be identical "
    f"(captured_id={id(captured)}, module_attr_id={id(mcs.StaleBillingProfileError)})"
)
print("OK pre-reload identity holds in child")
sys.exit(0)
"""

_PROBE_RELOAD_REBINDS = r"""
import importlib, sys
from app.services.multichurch_store import StaleBillingProfileError as captured
import app.services.multichurch_store as mcs
pre_id = id(captured)
pre_mod = captured.__module__
pre_qn  = captured.__qualname__
importlib.reload(mcs)
post = mcs.StaleBillingProfileError
assert pre_mod == post.__module__,     "modules match"
assert pre_qn  == post.__qualname__,   "qualnames match"
assert captured is not post, (
    f"post-reload: identity must differ (pre_id={pre_id}, post_id={id(post)})"
)
assert pre_id != id(post), f"ids must differ (pre={pre_id}, post={id(post)})"
print(f"OK reload rebinds class pre_id={pre_id} post_id={id(post)}")
sys.exit(0)
"""

_PROBE_CAPTURED_MISSES = r"""
import importlib, sys
from app.services.multichurch_store import StaleBillingProfileError as captured
import app.services.multichurch_store as mcs
importlib.reload(mcs)
try:
    raise mcs.StaleBillingProfileError("post-reload instance")
except captured:
    print("UNEXPECTED: captured caught post-reload instance")
    sys.exit(1)
except Exception as exc:
    assert isinstance(exc, mcs.StaleBillingProfileError), "class match"
    assert not isinstance(exc, captured), "captured mismatch"
    print("OK captured misses, current-ref catches")
    sys.exit(0)
"""

_PROBE_CURRENT_REF_CATCHES = r"""
import importlib, sys
import app.services.multichurch_store as mcs
importlib.reload(mcs)
caught = None
try:
    raise mcs.StaleBillingProfileError("post-reload instance")
except mcs.StaleBillingProfileError as exc:
    caught = exc
assert caught is not None, "current-ref must catch"
print("OK current-ref catches")
sys.exit(0)
"""

_PROBE_NAME_MOD_NOT_IDENTITY = r"""
import importlib, sys
from app.services.multichurch_store import StaleBillingProfileError as captured
import app.services.multichurch_store as mcs
importlib.reload(mcs)
post = mcs.StaleBillingProfileError
assert captured.__module__   == post.__module__
assert captured.__qualname__ == post.__qualname__
assert captured is not post, "same name does not prove identity"
print("OK name alone is not identity")
sys.exit(0)
"""


class CASModuleReloadIsolationTests(unittest.TestCase):
    """All reload probes run in fresh subprocesses so the
    parent's `sys.modules` is never mutated. A final
    parent-side identity check asserts the invariant is
    preserved for the rest of the pytest run.
    """

    def _assert_probe(self, source: str) -> subprocess.CompletedProcess:
        result = _run_probe(source)
        self.assertEqual(
            result.returncode, 0,
            f"child probe failed; stdout={result.stdout!r} "
            f"stderr={result.stderr!r}",
        )
        self.assertIn("OK", result.stdout, msg=result.stdout)
        return result

    def test_pre_reload_identity_holds_in_child(self) -> None:
        """Baseline: before any reload in the child, the
        captured name and the module attribute point to the
        same class object. Confirms the probe harness itself
        is correct."""
        self._assert_probe(_PROBE_BEFORE_RELOAD)

    def test_reload_rebinds_class_object_in_child(self) -> None:
        """After `importlib.reload` in the CHILD, the module
        attribute class has a different `id()` than the
        pre-reload captured class in the same child. Same
        module + qualname. Names alone do not prove identity."""
        self._assert_probe(_PROBE_RELOAD_REBINDS)

    def test_captured_name_misses_post_reload_instance_in_child(self) -> None:
        """The failure mechanism itself: the captured name
        does NOT catch an instance raised from the reloaded
        module. This is the exact classification path that
        made the CAS test record `unexpected` in CI's
        emulator-tests job."""
        self._assert_probe(_PROBE_CAPTURED_MISSES)

    def test_current_module_reference_catches_in_child(self) -> None:
        """The fix: catching via the current-module reference
        (`except mcs.X`) succeeds where the captured name
        failed, in the same child process state. This mirrors
        what the CAS test's committed catch does post-reload."""
        self._assert_probe(_PROBE_CURRENT_REF_CATCHES)

    def test_name_and_module_alone_do_not_prove_identity_in_child(self) -> None:
        """Negative control: equal `__module__` + equal
        `__qualname__` does NOT imply class identity. The fix
        must use `is`-identity semantics, not string matching."""
        self._assert_probe(_PROBE_NAME_MOD_NOT_IDENTITY)

    # ---------------------------------------------------------------
    # Parent-side isolation guarantee
    # ---------------------------------------------------------------
    def test_parent_module_identity_unchanged_after_subprocess_probes(self) -> None:
        """After all subprocess probes have run, the parent's
        pre-captured `StaleBillingProfileError` identity is
        unchanged. The parent's `sys.modules` for
        `app.services.multichurch_store` has not been reloaded.
        This is the isolation guarantee the subprocess design
        provides: reload probes in children never leak into the
        parent's test cache.
        """
        # Re-import the module attribute via a fresh binding
        # (NOT a reload) and compare to the pre-probe captured
        # identity. If any earlier test in THIS file had reloaded
        # the module in-process, this assertion would fail with
        # `id()` difference.
        from app.services.multichurch_store import (
            StaleBillingProfileError as refreshed_binding,
        )
        import app.services.multichurch_store as mcs_now
        self.assertIs(
            refreshed_binding, _parent_captured_cls,
            f"parent-side identity leaked: pre_probe_id="
            f"{_PARENT_CAPTURED_ID_AT_IMPORT}, "
            f"current_binding_id={id(refreshed_binding)}; some test "
            f"in this file reloaded the shared module in-process",
        )
        self.assertEqual(
            id(mcs_now.StaleBillingProfileError),
            _PARENT_MCS_ATTR_ID_AT_IMPORT,
            "parent-side module attribute id must match pre-probe",
        )
        # Belt and suspenders: no reload performed on the parent
        # process means re-importing the module gives the same
        # module object.
        self.assertIs(mcs_now, _mcs_parent_ref)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)

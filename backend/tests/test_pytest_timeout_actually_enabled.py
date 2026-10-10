"""Prove that pytest.mark.timeout(N) actually enforces N seconds.

v8 operator concern: a ``pytest.mark.timeout`` marker is a NO-OP when
``pytest-timeout`` is not installed in the venv — the marker gets
registered as an "unknown pytest marker" at warning level and silently
ignored. This test is a canary.

Approach:

1. ``test_pytest_timeout_is_registered`` — if ``pytest-timeout`` is
   installed, the ``timeout`` ini option is registered. We read the
   pytest config via the ``request.config`` fixture and assert the
   option is known. Failure means the plugin is NOT installed and
   every other ``pytest.mark.timeout(...)`` in the suite is a no-op.

2. ``test_sleep_longer_than_timeout_actually_fails`` — a test with
   a 1-second ``pytest.mark.timeout`` that sleeps 10 seconds. If
   pytest-timeout is active, pytest fires a timeout and the test
   FAILS. We mark it ``xfail(strict=True)`` so an xfail outcome
   means the timeout fired (expected). An unexpected pass (no
   timeout fired, sleep completes) means the plugin is NOT
   enforcing timeouts, and pytest reports strict-xfail-pass as a
   FAILURE.

Both signals together prove the mechanism is live. If either fails,
every other ``pytest.mark.timeout`` in the suite is suspect.
"""
from __future__ import annotations

import time

import pytest


def test_pytest_timeout_is_registered(request):
    """Pass only if the pytest-timeout plugin is actually loaded."""
    # The ``timeout`` ini option is registered by the pytest-timeout plugin.
    try:
        config_timeout = request.config.getini("timeout")
    except (ValueError, KeyError):
        pytest.fail(
            "pytest-timeout plugin is NOT installed in this venv. "
            "Every pytest.mark.timeout(...) marker in the suite is a no-op. "
            "Install with: pip install pytest-timeout"
        )
    # The ini value may be '' when no default timeout is set — that's fine;
    # getini succeeded, which means the option is registered, which means
    # the plugin is loaded. The @timeout(N) markers will fire per-test.
    assert config_timeout is not None  # tautological; the real check is above


@pytest.mark.xfail(strict=True, reason="pytest-timeout should fire within 1s; strict-xfail asserts enforcement is live")
@pytest.mark.timeout(1)
def test_sleep_longer_than_timeout_actually_fails():
    """A 10-second sleep under a 1-second timeout must fail — if it
    passes, the timeout mechanism is NOT enforcing anything."""
    time.sleep(10)


# v9 operator concern (operator #5): a strict-xfail outcome alone could
# reflect an UNRELATED test failure (e.g. the sleep raising a different
# exception, or any assertion failure inside the body). xfail(strict)
# merely requires that the test did not PASS; the reason could be
# anything. To uniquely identify "pytest-timeout fired", we run a sub-
# test in a subprocess and grep its output for pytest-timeout's exact
# emission string, "Failed: Timeout >". If the sub-test fails for any
# other reason the grep misses and this outer test fails.


def test_timeout_fires_with_specific_message(tmp_path):
    """Primary timeout-evidence test (v9).

    Writes a tiny one-test file to a tmp dir and runs it via the SAME
    venv's pytest in a subprocess. Captures stdout. Asserts the output
    contains pytest-timeout's exact failure-signature string so an
    unrelated failure cannot masquerade as a timeout.

    This avoids the pytester fixture (which requires
    ``pytest_plugins = ["pytester"]`` global registration) and runs in
    full process isolation, which also protects the outer suite from
    plugin side effects.
    """
    import subprocess
    import sys
    import os

    sub_test = tmp_path / "test_sub_timeout.py"
    sub_test.write_text(
        "import time\n"
        "import pytest\n"
        "\n"
        "@pytest.mark.timeout(1)\n"
        "def test_sleeps_longer_than_the_timeout():\n"
        "    time.sleep(10)\n"
    )

    # Use the SAME interpreter/venv as the outer test. sys.executable
    # points at the active python; its sibling pytest is in the venv.
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-v", str(sub_test)],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
        timeout=30,  # outer safety: subprocess itself must finish
    )

    combined = result.stdout + "\n" + result.stderr

    # pytest-timeout 2.4.0 emits the literal line
    # "Failed: Timeout (>1.0s) from pytest-timeout." (preceded by "E   "
    # in pytest -v output). The substring "from pytest-timeout" is
    # UNIQUE to that plugin's emission — no other failure mode (sleep
    # exception, assertion, etc.) produces it. If absent, the sub-test
    # either passed (no enforcement) or failed for an unrelated reason.
    expected_signature = "from pytest-timeout"
    assert expected_signature in combined, (
        f"pytest-timeout signature {expected_signature!r} was NOT in the "
        f"sub-test output. Either the plugin did not fire, or it fired with "
        f"a different message format. Sub-test exit={result.returncode}. "
        f"Combined output:\n{combined[-2000:]}"
    )
    # And the sub-test must have failed (returncode != 0). A zero exit
    # would mean the 10-second sleep completed and the plugin did nothing.
    assert result.returncode != 0, (
        f"Sub-test completed with exit 0 — the 10-second sleep ran to "
        f"completion, so the 1-second timeout did NOT fire. Output:\n"
        f"{combined[-2000:]}"
    )

"""Session-scoped fixtures for the resource-cleanup integration
harness. Every fixture here is either read-only introspection into the
Firestore emulator, or lifecycle management for the Deepgram stub and
the two backend processes.

Prerequisites (checked at collection time — the whole suite skips if
any are missing, and CI's harness job is configured to fail on
unexpected skips):

  - Redis reachable at REDIS_HOST:REDIS_PORT (default 127.0.0.1:6379).
  - Firestore emulator reachable at FIRESTORE_EMULATOR_HOST
    (default 127.0.0.1:8085).

Locally:

    docker run --rm -p 6379:6379 redis:7 &
    gcloud emulators firestore start --host-port=127.0.0.1:8085 \\
        --project=cleanup-track1-harness &
    pytest backend/tests/integration/resource_cleanup/

In CI: the `integration-tests` job wires both up (services + gcloud
setup) and enforces zero unexpected skips.
"""
from __future__ import annotations

import os
import pytest

from .harness.backend_process import (
    firestore_emulator_reachable,
    redis_reachable,
)


REDIS_HOST = os.getenv("HARNESS_REDIS_HOST", "127.0.0.1")
REDIS_PORT = int(os.getenv("HARNESS_REDIS_PORT", "6379"))
FIRESTORE_EMULATOR_HOST = os.getenv(
    "HARNESS_FIRESTORE_EMULATOR_HOST", "127.0.0.1:8085",
)
GCP_PROJECT = os.getenv("HARNESS_GCP_PROJECT", "cleanup-track1-harness")


def _harness_infra_ready() -> tuple[bool, str]:
    if not redis_reachable(host=REDIS_HOST, port=REDIS_PORT):
        return False, f"Redis not reachable at {REDIS_HOST}:{REDIS_PORT}"
    if not firestore_emulator_reachable(host=FIRESTORE_EMULATOR_HOST):
        return False, f"Firestore emulator not reachable at {FIRESTORE_EMULATOR_HOST}"
    return True, ""


_HARNESS_DIR = os.path.dirname(__file__)


def pytest_collection_modifyitems(config, items):
    """Skip THIS suite (only) if infrastructure is missing.

    Scoped to items whose test file lives under this conftest's
    directory — we must not mark every backend test as skipped just
    because the harness prereqs aren't up. CI's harness job fails the
    run on any unexpected skip inside our namespace (junit XML check),
    so this can't hide a broken harness in green CI.
    """
    ok, reason = _harness_infra_ready()
    if ok:
        return
    marker = pytest.mark.skip(reason=f"integration harness prereqs missing: {reason}")
    for item in items:
        item_path = str(getattr(item, "fspath", ""))
        if item_path.startswith(_HARNESS_DIR):
            item.add_marker(marker)


@pytest.fixture(scope="session")
def harness_config():
    return {
        "redis_host": REDIS_HOST,
        "redis_port": REDIS_PORT,
        "firestore_emulator_host": FIRESTORE_EMULATOR_HOST,
        "gcp_project": GCP_PROJECT,
    }


@pytest.fixture(scope="session")
def admin_store(harness_config):
    """A FirestoreMultiChurchStore wired to the emulator, used by
    tests for direct seeding / assertion reads."""
    from .harness.firestore_seed import build_admin_store
    return build_admin_store(
        emulator_host=harness_config["firestore_emulator_host"],
        project_id=harness_config["gcp_project"],
    )

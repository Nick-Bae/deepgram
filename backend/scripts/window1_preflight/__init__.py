"""Window 1 preflight helpers — read-only diagnostics the operator
runs from a detached worktree during the bootstrap merge window.

Two CLIs:
  - `count_live_rooms.py` — exhaustive Firestore collection-group
    count of `organizations/*/rooms/*` documents with
    `status == "live"`. Backs Step 1a of the runbook.
  - `roster_cross_check.py` — Cloud Monitoring
    `container/instance_count` vs Cloud Logging `reconciler_tick`
    equality on the union of active revisions. Backs Steps
    1b + 1c of the runbook.

Neither script writes to Firestore, Logging, Monitoring, Cloud
Run, or any other Google Cloud API. Both refuse execution when
the target project/database is not on the allowlist or when
`FIRESTORE_EMULATOR_HOST` conflicts with the target type
(production target vs emulator target)."""

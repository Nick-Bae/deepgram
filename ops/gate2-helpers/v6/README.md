# Gate 2 helpers v6 — review-only skeleton

**Status:** review-only. Full spec at [`docs/03-analysis/gate2-helpers-v6-spec.md`](../../../docs/03-analysis/gate2-helpers-v6-spec.md).

Not for use against production yet. See the dependency table (D1–D8) in the spec.

## What's here

| File | Purpose | State |
|---|---|---|
| `analyze_cloud_run.py` | The v5→v6 diff of `analyze_cloud_run`: expected-Redis parameter, literal-string checks, VPC egress mutex, AUTH secret binding, `=0` vs `=1` branches. | **Skeleton — v5 guards stubbed via `_run_v5_guards()`.** |
| `tests/test_analyze_cloud_run.py` | Fixture-driven unit tests for the v6 diff. `expected_redis` contract; literal REDIS_ENABLED; VPC mutex; AUTH binding; happy paths for `=0`, `=1` direct, `=1` connector. | **Complete for the diff — 15 tests.** |

## What's NOT here

Everything on the spec's dependency list (D1–D8) is intentionally missing so this branch stays reviewable in isolation. The enablement PR will:

- Copy the v5 `_run_v5_guards()` body verbatim (traffic split, latestReady/latestCreated, Ready + seconds-Ready, `ROOM_RECONCILER_ENABLED=1`, `--max-instances=1`).
- Add `log_checks.py` with runtime Cloud Logging queries for §4a-2 (adapter-up per instance, A5 paired condition, A8b first-probe deadline) and §4a-3 (no `redis_pubsub_*` / `redis_probe_*` events in the last 5 min).
- Add `gate2_preflight.sh` / `gate2_postdeploy.sh` drivers wired through the new signature.
- Copy the v5 test suite verbatim alongside `test_analyze_cloud_run.py` so the preserved v5 guards keep test coverage.

## Running the tests

```bash
cd ops/gate2-helpers/v6
python -m pytest tests/ -v
```

No external services required; the tests are pure fixture-driven.

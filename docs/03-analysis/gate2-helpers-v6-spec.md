# Gate 2 helpers v6 — specification

**Status:** review-only draft on branch `docs/gate2-helpers-v6-spec`. **v6 is PARTIAL** — the pieces this branch ships are listed in "What's here" (§0 below); the pieces that remain are listed alongside them and named in the dependencies table (§D). Not for merge or deployment yet — v6 approval is gated on the dependencies below (per PR #31 §5).

**Reference:** PR #31 (`docs/03-analysis/redis-fanout-rollout-proposal.md`) §5 spells out the acceptance criteria. This spec is the concrete v5→v6 diff plan the enablement PR will implement.

## §0. What this branch ships vs what remains

| Piece | This branch | Enablement PR |
|---|---|---|
| `analyze_cloud_run` v6 diff (contract, literal REDIS_ENABLED, VPC egress on serving revision, AUTH secretKeyRef structural validation) | **shipped** (skeleton with 35 fixture tests) | — |
| Preserved v5 guards (traffic split, latestReady/latestCreated identity, Ready + seconds-Ready, `ROOM_RECONCILER_ENABLED=1`, `--max-instances=1`) | **stubbed** via `_run_v5_guards()` | verbatim copy from `~/.gate2-helpers-v5/_gate2_lib.py` |
| `log_checks.py` runtime helpers for §4a-2 (adapter-up, A5 paired condition, A8b first-probe deadline) and §4a-3 (log-absence verification) | **not present** | full implementation |
| `gate2_preflight.sh` / `gate2_postdeploy.sh` drivers | **not present** | wrappers that call the new signature |
| Copy of v5 test suite alongside v6 tests | **not present** | `_gate2_test.py` + `_gate2_wrapper_test.py` copied |

## Scope

v5 (`~/.gate2-helpers-v5/`) hardcodes `analyze_cloud_run`'s guard as `redis_enabled == "0"` (see `_gate2_lib.py:525`). That guard is correct for Track 1 today but blocks the Gate 2 rerun that PR #31 gates on: the rerun runs with `REDIS_ENABLED=1` and needs the analyser to verify the full Redis-enabled acceptance surface, not merely refuse it.

v6 makes the expected Redis state configurable, and — when set to `=1` — adds the additional per-revision checks PR #31 §5 requires. Every v5 check that isn't Redis-specific is preserved verbatim.

## Dependencies (hard prerequisites)

Each of the following MUST have landed on `main` before v6 can be used for a real Gate 2 rerun. Marked here so the operator can check preconditions before invoking any v6 helper.

| # | Dependency | Ship-vehicle | Verified by |
|---|---|---|---|
| D1 | Redis-enabled Cloud Run revision serving traffic | Enablement PR (task #137) | Cloud Run traffic status |
| D2 | Structured JSON emission (`_emit`) in `redis_pubsub.py` | **PR #34** | `test_redis_pubsub.py::RedisPubSubStructuredEmissionTests` |
| D3 | In-process probe task + `redis_probe_ok`/`redis_probe_failed` events | **PR #35** | `test_redis_pubsub.py::RedisPubSubProbeLoopTests` |
| D4 | Log-based metrics + alert policies (A5, A5-legacy, A6, A7, A8a) | Task #135 — separate operator PR | `ops/monitoring/reconciler/` apply |
| D5 | `REDIS_ENABLED=1` env on the serving revision (via §4d window) | Enablement PR (task #137) | `gcloud run revisions describe` returns `REDIS_ENABLED=1` |
| D6 | Memorystore Standard-tier instance + VPC egress path attached to Cloud Run + **"CPU always allocated"** | Task #136 (Cloud Run config + Memorystore provisioning) | `gcloud run services describe` shows connector or Direct VPC subnet AND `spec.template.metadata.annotations."run.googleapis.com/cpu-throttling"="false"` — PR #31 §3 W2 requires always-allocated CPU so the 30 s probe task fires reliably |
| D7 | AUTH secret binding matches the Memorystore instance | Task #136 | Revision env: `REDIS_PASSWORD` present iff instance has AUTH, absent otherwise |
| D8 | Production `deploy_gate` writer script exists so §4d step 1/6 can execute | Task #133 | Script lands with its own regression tests |
| D9 | **Probe auto-enable in singleton startup path** — PR #35 makes the probe OPT-IN via `enable_probe_task()`. For a real Gate 2 rerun the singleton must call `enable_probe_task()` unconditionally when `REDIS_ENABLED=1`. Merging PR #35 alone does NOT supply this. | Task #134 — separate PR | Adapter's `start()` schedules `_probe_task` when Redis is enabled; F-27-adjacent test confirms in-CI |

v6 can be run against a `GATE2_EXPECTED_REDIS=0` fixture ANY time (that's the current production shape). The `=1` path is only meaningful after D1-D9 are ALL true — merging PR #34+#35 covers D2+D3 but NOT D6 (CPU), D9 (auto-enable), D4/D7/D8/D1/D5.

## Contract changes vs v5

### 1. `analyze_cloud_run` accepts an expected-Redis parameter

Signature grows one keyword-only argument:

```python
def analyze_cloud_run(
    service_desc: dict,
    revision_desc: dict,
    stabilization_sec: int,
    *,
    expected_redis: str,               # NEW — "0" or "1"
    vpc_mode: str | None = None,       # NEW — "connector" or "direct" when expected_redis == "1"
    now: datetime | None = None,
) -> tuple[dict[str, Any], list[str]]:
```

`expected_redis` is driven by the helper env `GATE2_EXPECTED_REDIS` (mandatory); `vpc_mode` by `GATE2_VPC_MODE` (mandatory when `GATE2_EXPECTED_REDIS=1`). Neither has a default — v5's hardcoded `=0` is not a v6 fallback because the fallback silently reverting an operator's intent is exactly the shape PR #31 §5 rejects.

### 2. Redis-state check is now conditional

v5 check (`_gate2_lib.py:525`):

```python
if redis_enabled not in ("0", "false", "False"):
    guards.append(f"REDIS_ENABLED must be 0; got {redis_enabled!r}")
```

v6 replacement:

```python
expected_literal = expected_redis  # "0" or "1"
if redis_enabled != expected_literal:
    guards.append(
        f"REDIS_ENABLED must be exactly {expected_literal!r}; got {redis_enabled!r}"
    )
```

Note `redis_enabled != expected_literal` rejects `"false"`/`"False"`/`"true"`/`"True"` — v5 allowed the truthy string variants for `=0`. v6 requires the literal `"0"` or `"1"` because Cloud Run's env storage is text and v6's downstream metric filters key on that literal too. Ops's set/clear script (task #133) writes the literal.

### 3. `=1` adds VPC egress + AUTH secret checks

When `expected_redis == "1"`, guard the following additional invariants (see PR #31 §4c step 2 and §5):

```python
if expected_redis == "1":
    # Mutually exclusive; both is a stop.
    connector = _connector_annotation(service_desc)
    direct_subnet = _direct_vpc_subnet(service_desc)
    if vpc_mode == "connector":
        if not connector:
            guards.append("expected connector VPC egress; no vpc-access-connector attached")
        if direct_subnet:
            guards.append("connector + Direct VPC egress both attached; must be exactly one")
    elif vpc_mode == "direct":
        if not direct_subnet:
            guards.append("expected Direct VPC egress; no egress subnet attached")
        if connector:
            guards.append("Direct VPC egress + connector both attached; must be exactly one")
    else:
        guards.append(f"GATE2_VPC_MODE must be 'connector' or 'direct'; got {vpc_mode!r}")

    # AUTH secret binding matches the instance state (D7).
    # Instance AUTH state is not observable from the service desc
    # alone — the operator supplies it as REDIS_INSTANCE_HAS_AUTH.
    # v6 refuses to guess.
    has_auth = os.environ.get("REDIS_INSTANCE_HAS_AUTH")
    if has_auth not in ("0", "1"):
        guards.append(
            "REDIS_INSTANCE_HAS_AUTH must be '0' or '1' when expected_redis=1"
        )
    else:
        password_binding = _redis_password_binding(revision_desc)
        if has_auth == "1" and not password_binding:
            guards.append("REDIS_INSTANCE_HAS_AUTH=1 but REDIS_PASSWORD not bound on the serving revision")
        if has_auth == "0" and password_binding:
            guards.append("REDIS_INSTANCE_HAS_AUTH=0 but REDIS_PASSWORD is still bound on the serving revision (stale)")
```

### 4. Runtime-log checks (separate module, invoked from the postdeploy path)

Static analysis of `service_desc`/`revision_desc` is not enough. PR #31 §4a-2 and §5 also require the postdeploy helper to run Cloud Logging queries and assert:

- **Adapter-up per instance** — for every rostered `jsonPayload.instance_id` on the serving revision, at least one entry matches `jsonPayload.event="redis_pubsub_started"` OR `jsonPayload.event="redis_pubsub_reconnected"`. Requiring only `_started` would spuriously fail an instance that started while Memorystore was briefly unreachable — see PR #31 §4a-2 W4.
- **A5 paired condition** — `jsonPayload.event="redis_pubsub_initial_connect_failed"` on an instance MUST be followed within 5 min by `jsonPayload.event="redis_pubsub_reconnected"` on the SAME `jsonPayload.instance_id`. A lone `redis_pubsub_initial_connect_failed` is a stop. **Event names come from the ACTUAL adapter emissions** (see `backend/app/services/redis_pubsub.py::_emit` catalogue on PR #34) — the older aggregate/metric names `redis_pubsub_startup_failed` / `redis_pubsub_reconnect_successes` were metric-filter labels, never event names emitted by the adapter, and using them in the log query would match nothing.
- **A8b first-probe deadline** — for every rostered instance, at least one `jsonPayload.event="redis_probe_ok"` MUST land within `PROBE_FIRST_DEADLINE_SEC` (default 90 s) of the instance's first `jsonPayload.event="reconciler_tick"`. Missing = UNRESOLVED = window fails.

These live in a new `ops/gate2-helpers/v6/log_checks.py` module (skeleton exists in this branch; full implementation lands with D3+D4).

### 5. `=0` path adds explicit config verification

v5 doesn't need this because `=0` was hardcoded. v6's `=0` path is the ROLLBACK-direction acceptance check (PR #31 §4a-3), and per Y5/Z1 in the proposal review, absence of Redis log activity alone cannot prove the flag was actually flipped. v6 `=0` therefore ALSO checks:

- The `REDIS_ENABLED=0` env is present on the serving revision (already covered by the modified check in §2 above — but note v5 doesn't refuse `""` or `"unset"`; v6 does, per the literal-check rule).
- No `jsonPayload.event` starting with `redis_pubsub_` and no `redis_probe_*` events in the last 5 min from any instance on the serving revision.

### 6. Fixture-driven tests for both paths

`ops/gate2-helpers/v6/tests/test_analyze_cloud_run.py` MUST cover:

- **Happy path — `expected_redis="0"`:** existing v5 fixtures (single-revision serving, latestReady==latestCreated==serving, Ready=True + seconds-Ready>stab, `REDIS_ENABLED=0`, `ROOM_RECONCILER_ENABLED=1`, `--max-instances=1`) all produce empty guards.
- **Happy path — `expected_redis="1"`, `vpc_mode="direct"`:** as above with `REDIS_ENABLED=1`, a Direct VPC egress subnet, `REDIS_INSTANCE_HAS_AUTH=1` and matching `REDIS_PASSWORD` binding → empty guards.
- **Happy path — `expected_redis="1"`, `vpc_mode="connector"`:** as above with a `vpc-access-connector` annotation → empty guards.
- **Refuses `"true"`/`"True"`/`"1"` (as literal) when expected is `"0"`.**
- **Refuses `"0"`/`"false"` when expected is `"1"`.**
- **Refuses connector when `vpc_mode="direct"`** (both attached is a stop).
- **Refuses Direct VPC subnet when `vpc_mode="connector"`**.
- **Refuses stale REDIS_PASSWORD when `REDIS_INSTANCE_HAS_AUTH=0`.**
- **Refuses missing REDIS_PASSWORD when `REDIS_INSTANCE_HAS_AUTH=1`.**
- **Refuses missing `GATE2_VPC_MODE` when `expected_redis="1"`.**
- **Every v5 guard still fires** when its input is bad (traffic split, non-Ready revision, wrong max-instances, etc.).

`test_log_checks.py` covers the log-query helpers with mocked Cloud Logging responses (out of scope for this review-only branch — lands with D3+D4).

## v6 ships as its own PR

- Depends-on: D2 (PR #34), D3 (PR #35), D4/D6/D7 (operator PRs), D8 (task #133).
- Does not depend on: D1/D5 (enablement itself). v6 can be reviewed + merged before the first `=1` deploy; running v6 against a production `=1` service is what waits on D1/D5.
- Ships with the fixture tests as its acceptance evidence.

## Rollback

v6 is backward-compatible with v5's `expected_redis="0"` invocation shape once the driver script sets `GATE2_EXPECTED_REDIS=0` explicitly. There is no code-side rollback needed if v6 is later reverted — v5 continues to work unchanged from `~/.gate2-helpers-v5/`.

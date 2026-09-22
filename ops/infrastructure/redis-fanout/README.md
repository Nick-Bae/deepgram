# Redis fanout — infrastructure config

Declarative source-of-truth for the Cloud Run / Memorystore /
VPC / Secret Manager configuration the Redis pub/sub rollout
requires. Config + validator + planner only. **No writes to
Google Cloud** happen from CI or from `apply.py`; `--apply`
returns rc=6 with a "planning only" message until task #137's
enablement PR lands.

## Contents

```
ops/infrastructure/redis-fanout/
├── cloudrun.yaml            Cloud Run service knobs
├── memorystore.yaml         Cloud Memorystore Redis instance
├── vpc.yaml                 VPC egress path (Direct preferred)
├── secrets.yaml             Secret Manager bindings
├── manifest.yaml            Cross-reference + pinned constraints
├── validate.py              Structural checks; CLI + library
├── apply.py                 Plan-only default; --apply → rc=6
└── README.md
```

## How the four resource files map to the rollout doc

| File | Spec anchor |
|---|---|
| `cloudrun.yaml` | §4c step 2/4/5 — env, VPC attachment, CPU, max-instances |
| `memorystore.yaml` | §4c step 3 — Standard tier, HA, AUTH |
| `vpc.yaml` | §4c step 2 — Direct VPC egress preferred; connector fallback |
| `secrets.yaml` | §4c step 4 — Secret Manager binding for AUTH |

Every knob the reviewer named in task #136's briefing lives in
one of these four YAMLs plus the manifest's `pinned_constraints`
list.

## Pinned constraints

`manifest.yaml`'s `pinned_constraints` block names the fields
that MUST hold on `main` until task #137's enablement PR:

- `cloudrun.env[REDIS_ENABLED].value = "0"` — the Track 1 gate
  the reviewer explicitly demanded stays pinned.
- `cloudrun.env[REDIS_ENABLED].kind = "literal"` — refuses a
  secretKeyRef binding for this key.
- `cloudrun.spec.max_instances = 1` — Track 1 Gate 2 constraint.
- `cloudrun.spec.cpu_allocation = "always"` — PR #31 §3 A8b's
  90 s first-probe deadline.
- `cloudrun.spec.ingress = "internal-and-cloud-load-balancing"`
  — BOOT-1 session-start gate no-bypass rule.

Drift on any of these fails `validate.py`, refuses `apply.py`
planning, and fails CI.

## Cross-resource invariants

`manifest.yaml`'s `cross_resource_invariants` block enforces
that pairs / triples of fields across resources agree:

- VPC mode must match the Cloud Run template annotations.
- Memorystore `auth_enabled` must match `secrets.redis_password.present`.
- Cloud Run `REDIS_HOST.value_from` must point at
  `memorystore.host_binding`.
- `transit_encryption_mode` must be `DISABLED` until the client
  is switched to `ssl=True` in the same commit.

## Operator use — offline preview (default)

```
python ops/infrastructure/redis-fanout/apply.py \
    --project sturdy-dogfish-472313-k6
```

Prints the offline desired-state preview and exits 0 on a clean
manifest. `--project` must be on `manifest.allowed_projects`;
anything else refuses with rc=4 before any preview runs.

## Operator use — apply (gated; not in this PR)

```
python ops/infrastructure/redis-fanout/apply.py \
    --project sturdy-dogfish-472313-k6 \
    --apply --confirm 'I understand this affects production infrastructure'
```

Currently exits rc=6 with a "planning + validation only"
message. The SDK snapshot + write paths land in task #137's
enablement PR.

## Return codes

| Code | Meaning |
|---|---|
| 0 | Offline preview printed, nothing to refuse |
| 2 | (Reserved) missing Google Cloud SDK — introduced with task #137 |
| 4 | Refuse — allowlist miss or static-check failure |
| 5 | `--apply` supplied without correct `--confirm` |
| 6 | `--apply` requested — this PR ships planning + validation only |

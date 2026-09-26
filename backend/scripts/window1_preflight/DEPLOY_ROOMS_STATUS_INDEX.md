# Deploy the `rooms.status` collection-group index — operator guide

Versioned procedure that applies PR #42's Firestore field
override to production. **This document is deliberately thin**:
the fail-closed logic lives in `deploy_rooms_status_index.py`
and is exercised by `backend/tests/test_deploy_rooms_status_index.py`
under a fake-CLI harness. Do not rewrite the logic here in
shell — invoke the driver.

## Versions

| | |
|---|---|
| Driver script version | `1.0.0` (`SCRIPT_VERSION` in `deploy_rooms_status_index.py`) |
| Approved PR #42 SHA to pin | `<PR42_HEAD_SHA>` — fetch and record with `gh pr view 42 --json headRefOid -q .headRefOid` |
| Firebase CLI version required | `13.19.0` (pass as `--firebase-tools-version-pin`; adjust when the operator's host is upgraded and the fixture tests are re-run) |
| Google Cloud SDK version used to validate | `Google Cloud SDK 581.0.0` |

## Preconditions the driver enforces

Every one of these produces a distinct exit code with a JSON
payload the operator captures verbatim into the audit trail —
no ambiguity, no retry-blindly:

- `firebase` binary present AND `firebase --version` exactly matches `--firebase-tools-version-pin`
- `gcloud` binary present
- `--worktree` exists, HEAD == `--pr42-sha`, worktree is clean
- `--audit-dir` exists, mode `700`, empty at driver start
- `firebase.json` targets `worship-translation` @ `us-central1` and points at the root `firestore.indexes.json`
- `.firebaserc` default project is `sturdy-dogfish-472313-k6`
- PR #42's `firestore.indexes.json` passes the static invariants (exactly one `rooms.status` override with the four expected entries; no forbidden CG DESC / CG ARRAY_CONTAINS)

## Exit codes

Mirrored in `RC` inside the driver. Every non-zero exit prints
exactly one JSON line on stdout with `rc` + `outcome` + `reason`.

| RC | Meaning |
|---|---|
| 0 | Success — deploy committed, index READY, all diffs match |
| 1 | Usage / argparse |
| 2 | Preconditions (CLI missing, wrong version, dirty/wrong worktree, non-empty audit dir) |
| 3 | Pre-snapshot failure |
| 4 | Target confirmation failure |
| 5 | Static invariants failed on PR #42's file |
| 6 | `firebase deploy` exited non-zero (or timed out) |
| 7 | Post-snapshot present, but pre→post diff violates invariants (unexpected add / remove / wrong-shape new override) |
| 8 | Polling failed — `NEEDS_REPAIR`, `MISSING`, or exceeded timeout |
| 9 | Final snapshot missing the READY CG_ASC entry |
| 99 | Internal / unexpected |

## Snapshots the driver captures

All under `--audit-dir`, all mode 700, all hashed:

- `pre/` — database-wide composite index list; per-collection-group field-override lists across the known set (`organizations`, `services`, `rooms`, `members`, `invites`, `usage`, `sermons`); `rooms.status` describe; `manifest.json`; `snapshot.sha256`.
- `post/` — same shape, taken by an atexit + signal-handler trap so it fires on success, non-zero, SIGINT, and SIGTERM. The `test_sigint_mid_deploy_still_writes_post_snapshot` fixture proves the trap fires under operator Ctrl-C.
- `poll/poll.log` — every polled state observation.
- `final/` — same shape as pre and post, taken after READY; verifies the CG_ASC entry landed with `state: READY`.
- `deploy/deploy.stdout`, `deploy/deploy.stderr`, `deploy/deploy.rc` — the exact firebase deploy output.

## Recovery is a compensating change, NOT a git revert

If the diff of post-snapshot vs pre-snapshot shows anything
other than "exactly the intended rooms.status override was
added", the driver dies with rc=7 and preserves both
snapshots. Recovery is a NEW, separately reviewed,
separately authorized change constructed from the pre-snapshot
as the source of truth for what production was — see PR #42's
prior R2 checklist for the compensating-change procedure. A
`git revert` of PR #42 is **not** an acceptable rollback.

## How to invoke

```bash
# 1. Fetch and pin the reviewed PR #42 head.
PR42_HEAD_SHA=$(gh pr view 42 --json headRefOid -q .headRefOid)

# 2. Build a detached worktree at that SHA.
PR42_WORKTREE=$(mktemp -d -t pr42-deploy.XXXXXXXX)
git fetch origin chore/rooms-status-collection-group-index
git worktree add --detach "$PR42_WORKTREE" "$PR42_HEAD_SHA"

# 3. Owner-only audit dir.
umask 077
PR42_AUDIT_DIR=$(mktemp -d -t pr42-audit.XXXXXXXX)

# 4. Verify the environment WITHOUT deploying (dry-run):
python3 backend/scripts/window1_preflight/deploy_rooms_status_index.py \
  --audit-dir "$PR42_AUDIT_DIR" \
  --worktree "$PR42_WORKTREE" \
  --pr42-sha "$PR42_HEAD_SHA" \
  --firebase-tools-version-pin 13.19.0 \
  --dry-run

# 5. If dry-run reported rc=0 outcome=dry_run_ready AND the reviewer
#    has explicitly authorized the deploy, take a fresh audit dir
#    and run without --dry-run:
umask 077
PR42_AUDIT_DIR=$(mktemp -d -t pr42-audit.XXXXXXXX)
python3 backend/scripts/window1_preflight/deploy_rooms_status_index.py \
  --audit-dir "$PR42_AUDIT_DIR" \
  --worktree "$PR42_WORKTREE" \
  --pr42-sha "$PR42_HEAD_SHA" \
  --firebase-tools-version-pin 13.19.0
```

The dry-run mode is safe against production: it runs preconditions,
pre-snapshot, target confirmation, and PR #42 static invariants,
then stops. It does not invoke `firebase deploy`, does not poll,
does not take post/final snapshots.

## Test evidence for R2

The driver's failure modes are exercised by
`backend/tests/test_deploy_rooms_status_index.py` under a
fake-CLI harness in `backend/tests/deploy_index_fixtures/`. The
suite covers:

- success — clean deploy, CG_ASC READY, all diffs match
- dry-run is a preparation checkpoint (skips deploy + post-diff + poll + final-diff)
- deploy command failure — non-zero exit; post-snapshot still fires
- unexpected deletion of an unrelated composite
- unexpected addition of an unrelated composite
- polling observes NEEDS_REPAIR
- CG_ASC never appears; polling hits timeout (MISSING)
- polling times out on stuck CREATING
- preconditions: bad firebase version pin, dirty worktree, HEAD mismatch, non-empty audit dir
- target confirmation: wrong default project
- static invariants: PR #42 config is malformed
- SIGINT mid-deploy — trap still writes a post-snapshot

Run locally with:

```
pytest backend/tests/test_deploy_rooms_status_index.py -v
```

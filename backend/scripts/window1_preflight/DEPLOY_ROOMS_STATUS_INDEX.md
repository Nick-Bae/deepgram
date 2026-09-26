# Deploy the `rooms.status` collection-group index — operator guide

Versioned procedure that applies PR #42's Firestore field
override to production. **This document is deliberately thin**:
the fail-closed logic lives in `deploy_rooms_status_index.py`
(driver) and `backend/tests/test_deploy_rooms_status_index.py`
(fixture suite). Do not rewrite the logic here in shell —
invoke the driver.

## Versions

| | |
|---|---|
| Driver script version | `2.0.0` (`SCRIPT_VERSION` in `deploy_rooms_status_index.py`) |
| Approved PR #42 SHA to pin | recorded independently by the reviewer; passed as `--reviewer-approved-sha` and MUST equal `--pr42-sha` |
| Script sha256 pin | recorded independently by the reviewer; passed as `--script-sha256`; driver re-hashes itself and refuses on mismatch |
| Firebase CLI version required | `13.19.0` (passed as `--firebase-tools-version-pin`; bump requires re-running the fixture suite) |
| Google Cloud SDK version used to validate | `Google Cloud SDK 581.0.0` |

## Preconditions the driver enforces

Every one of these produces a distinct exit code with a JSON
payload the operator captures verbatim into the audit trail —
no ambiguity, no retry-blindly:

- `firebase` binary present AND `firebase --version` exit code == 0 AND its stdout equals `--firebase-tools-version-pin`
- `gcloud` binary present
- `--worktree` exists, HEAD == `--pr42-sha`, worktree is clean
- **Driver file lives inside `--worktree`** at its known path (`backend/scripts/window1_preflight/deploy_rooms_status_index.py`)
- **Driver file sha256** equals `--script-sha256`
- **`--reviewer-approved-sha` equals `--pr42-sha`** — prevents a newer, unreviewed head from being deployed
- `--audit-dir` exists, mode `700`, empty at driver start
- **Remote database confirmation via `gcloud firestore databases describe`** — `name`, `type=FIRESTORE_NATIVE`, `locationId=us-central1` all match
- `firebase.json` targets `worship-translation` @ `us-central1` and points at the root `firestore.indexes.json`
- `.firebaserc` default project is `sturdy-dogfish-472313-k6`
- PR #42's `firestore.indexes.json` passes the R3 strict-schema invariants (no unknown keys, no duplicate entries, exact type checks; exactly one `rooms.status` override with the four expected entries; no forbidden CG DESC / CG ARRAY_CONTAINS)
- **Pre-deploy semantic delta** — the LOCAL config, if applied, must take production from the pre-snapshot to (pre + intended `rooms.status`) with NOTHING else changed. Any pre-existing composite index or unrelated field override that firebase deploy would delete → refuse

## Exit codes

| RC | Meaning |
|---|---|
| 0 | Success — deploy committed, index READY, all diffs match |
| 1 | Usage / argparse (incl. non-positive-finite timeouts) |
| 2 | Preconditions (CLI missing, wrong version, script/reviewer/worktree SHA mismatch, dirty worktree, non-empty audit dir) |
| 3 | Pre-snapshot failure |
| 4 | Target confirmation failure (incl. remote database mismatch) |
| 5 | Static invariants OR pre-deploy delta failed |
| 6 | `firebase deploy` exited non-zero (or timed out) |
| 7 | Post-snapshot present, but pre→post diff violates invariants |
| 8 | Polling failed — `NEEDS_REPAIR`, MISSING after post-deploy (hard-stop), or timeout |
| 9 | Final snapshot: composites drift, unrelated field-override drift, or CG_ASC not READY |
| 99 | Internal / unexpected |

## Snapshots the driver captures

All under `--audit-dir`, all mode 700, all hashed:

- `pre/` — database-wide composite index list; **database-wide** field-override list (no `--collection-group` filter — R3 correction); `rooms.status` describe; `database.json` (`gcloud firestore databases describe`); `manifest.json`; `snapshot.sha256`.
- `pre-deploy-delta.json` — the delta-report proving the local config, if applied, changes only what's intended.
- `post/` — same shape as pre; taken by an atexit + signal-handler trap. **The trap terminates the deploy process group BEFORE snapshotting** (R3 finding 6) so firebase cannot keep mutating production after a signal.
- `poll/poll.log` — every polled state observation.
- `final/` — same shape as pre and post; taken after READY; diffed FULLY against pre for composites AND field overrides.
- `deploy/deploy.stdout`, `deploy/deploy.stderr`, `deploy/deploy.rc`, `deploy/cmd.txt` — the exact firebase deploy invocation + output.

## Signal handling

Driver installs an atexit hook + SIGINT/SIGTERM handler that
fires exactly once. The handler:

1. Terminates the deploy child's process group (SIGTERM, then SIGKILL after 5 s if still alive) so firebase cannot keep mutating.
2. Takes the post-snapshot.
3. Re-raises the original signal so the process exits with the canonical signal exit code.

The fixture `test_sigint_mid_deploy_still_writes_post_snapshot`
proves the trap fires under operator Ctrl-C.

## Recovery is a compensating change, NOT a git revert

If the post-diff, poll, or final-diff surfaces a violation, the
driver dies with the appropriate RC and preserves both pre- and
post-snapshots. Recovery is a NEW, separately reviewed,
separately authorized change constructed from the pre-snapshot
as the source of truth for what production was — see PR #42's
prior R2 checklist for the compensating-change procedure. A
`git revert` of PR #42 is **not** an acceptable rollback.

## How to invoke

**Invoke the driver FROM the pinned worktree** (not the
operator's checkout, not a copied copy elsewhere):

```bash
# 1. Reviewer-approved artifacts to hand-carry:
#    - APPROVED_SHA: the exact PR #42 head commit that was reviewed
#    - SCRIPT_SHA256: sha256 of the reviewed driver file
APPROVED_SHA="03a085c9…"   # replace with the reviewer's exact hex
SCRIPT_SHA256="e585c08e…"  # sha256 of the reviewed driver file

# 2. Detached worktree pinned to APPROVED_SHA.
PR42_WORKTREE=$(mktemp -d -t pr42-deploy.XXXXXXXX)
git fetch origin chore/rooms-status-collection-group-index

# Independent equality check: what the PR head IS must equal
# what the reviewer approved. Refuses a newer head silently.
LIVE_HEAD=$(gh pr view 42 --json headRefOid -q .headRefOid)
test "$LIVE_HEAD" = "$APPROVED_SHA" || {
  echo "PR #42 head $LIVE_HEAD != reviewer-approved $APPROVED_SHA"; exit 2;
}

git worktree add --detach "$PR42_WORKTREE" "$APPROVED_SHA"

# 3. Owner-only audit dir.
umask 077
PR42_AUDIT_DIR=$(mktemp -d -t pr42-audit.XXXXXXXX)

# 4. Verify the environment WITHOUT deploying (dry-run):
python3 "$PR42_WORKTREE/backend/scripts/window1_preflight/deploy_rooms_status_index.py" \
  --audit-dir "$PR42_AUDIT_DIR" \
  --worktree "$PR42_WORKTREE" \
  --pr42-sha "$APPROVED_SHA" \
  --reviewer-approved-sha "$APPROVED_SHA" \
  --script-sha256 "$SCRIPT_SHA256" \
  --firebase-tools-version-pin 13.19.0 \
  --dry-run

# 5. If dry-run reported rc=0 outcome=dry_run_ready AND the reviewer
#    has explicitly authorized the deploy, take a fresh audit dir
#    and run without --dry-run.
umask 077
PR42_AUDIT_DIR=$(mktemp -d -t pr42-audit.XXXXXXXX)
python3 "$PR42_WORKTREE/backend/scripts/window1_preflight/deploy_rooms_status_index.py" \
  --audit-dir "$PR42_AUDIT_DIR" \
  --worktree "$PR42_WORKTREE" \
  --pr42-sha "$APPROVED_SHA" \
  --reviewer-approved-sha "$APPROVED_SHA" \
  --script-sha256 "$SCRIPT_SHA256" \
  --firebase-tools-version-pin 13.19.0
```

The dry-run mode is safe against production: it runs
preconditions, pre-snapshot, target confirmation, PR #42 static
invariants, AND the pre-deploy semantic delta, then stops. It
does not invoke `firebase deploy`, does not poll, does not take
post/final snapshots.

## Test evidence for R3

`backend/tests/test_deploy_rooms_status_index.py` exercises the
driver's failure modes under a fake-CLI harness in
`backend/tests/deploy_index_fixtures/`. The R3 suite adds:

- **finding 1/9**: unknown-collection-group override in pre-state → pre-deploy delta refuses
- **finding 3**: pre-deploy delta refuses on unrelated remote override
- **finding 3**: pre-deploy delta refuses on remote composite present
- **finding 3**: dry-run runs pre-deploy delta and refuses bad pre-state
- **finding 5**: `--reviewer-approved-sha` != `--pr42-sha` → rc=2
- **finding 5**: `--script-sha256` mismatch → rc=2
- **finding 7**: MISSING observation after post-deploy → immediate rc=8 poll_error
- **finding 8**: `gcloud firestore databases describe` locationId mismatch → rc=4
- **finding 8**: databases describe name mismatch → rc=4
- **finding 8**: zero / NaN timeout rejected by argparse (rc≠0)
- **finding 8**: strict schema — unknown top-level key rejected
- **finding 8**: strict schema — duplicate entries rejected

Plus the R2 fixtures updated to the R3 nested-shape state model:
success, dry-run preparation checkpoint, deploy command failure,
NEEDS_REPAIR, polling timeout, dirty worktree, HEAD mismatch,
non-empty audit dir, wrong target project, bad firebase version
pin, malformed PR #42 config, unexpected composite deletion
(pre-deploy delta refusal), unexpected composite addition
(post-diff), SIGINT mid-deploy.

Local run: 28/28 fixture tests + 10/10 static tests = **38/38 pass**.

Run locally with:

```
pytest backend/tests/test_deploy_rooms_status_index.py \
       backend/tests/test_firestore_indexes_config.py -v
```

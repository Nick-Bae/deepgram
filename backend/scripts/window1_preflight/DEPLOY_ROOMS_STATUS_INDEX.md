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
| Driver script version | `3.2.0` (`SCRIPT_VERSION` in `deploy_rooms_status_index.py`) |
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

## Signal handling and process-group quiescence

The driver treats the deploy child's whole process group as a
single lifecycle unit. Every phase enforces quiescence before
the driver moves on:

1. **PGID is captured at Popen** — the child is launched with
   `start_new_session=True`, so it is process-group leader
   immediately and its PGID equals its PID. The driver records
   that value synchronously (R6 finding 1). Later trap code
   never re-derives the PGID via `os.getpgid(popen.pid)` — that
   call would fail with `ProcessLookupError` on a reaped leader,
   which would leave a surviving descendant undetected.
2. **A whole-PG drain runs on every `_deploy` exit path** —
   normal rc=0, non-zero exit, timeout, and signal-triggered
   teardown (R6 finding 1). The drain sends SIGTERM to the PG,
   waits up to 5 s for every non-zombie member to disappear
   from `/proc`, SIGKILLs any survivor, and waits up to 2 s
   more. Zombie processes (`state == 'Z'`) are excluded — they
   cannot run user code (R5 finding 1).
3. **`trap-failure.txt` records the survivor list** if any
   process outlives SIGKILL, and the driver exits **rc=6
   (`deploy_group_quiescence_failed`)** — an rc=0 result is
   not returned when quiescence cannot be established.
4. The signal-handler trap additionally takes the post-snapshot
   and re-raises the original signal so the process exits with
   the canonical signal exit code.

Fixture coverage:

- `R6DeployGroupQuiescenceTests` — leader-exits-early race
  (fake firebase forks a same-PG grandchild that installs
  `SIG_IGN` for SIGTERM, then exits rc=0 immediately) and
  normal-rc=0 race (successful deploy leaves a delayed-mutation
  grandchild).
- `R5ProcessGroupDrainTests` — grandchild ignoring SIGTERM
  during a SIGINT-mid-deploy.
- `test_sigint_mid_deploy_terminates_process_group_and_prevents_late_mutation`
  (R4) — default-SIGTERM-handling grandchild during a SIGINT.

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
# 1. Reviewer-approved artifacts to hand-carry. DO NOT paste
#    stale example SHAs — the reviewer's approval message
#    contains the exact values for this deploy round.
APPROVED_SHA="<REVIEWER_APPROVED_SHA_HERE>"    # 40-char hex
SCRIPT_SHA256="<REVIEWER_APPROVED_SCRIPT_SHA256_HERE>"  # 64-char hex

# 2. Detached worktree pinned to APPROVED_SHA.
PR42_WORKTREE=$(mktemp -d -t pr42-deploy.XXXXXXXX)
git fetch origin main chore/rooms-status-collection-group-index

# R4 finding 6: prove the reviewed head is BASED ON current
# origin/main. A stale/rebased reviewed head that predates
# main's advancement would deploy code that doesn't reflect
# the current tree, and could silently miss composites or
# rules changes that landed after the PR was reviewed.
git merge-base --is-ancestor origin/main "$APPROVED_SHA" || {
  echo "APPROVED_SHA $APPROVED_SHA does not contain origin/main HEAD" >&2
  echo "→ rebase the PR onto main, get fresh reviewer approval" >&2
  exit 2
}

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

## Test evidence

`backend/tests/test_deploy_rooms_status_index.py` exercises the
driver's failure modes under a fake-CLI harness in
`backend/tests/deploy_index_fixtures/`. Coverage evolved across
rounds:

- **R3** — nested-shape state model, pre-deploy semantic delta,
  reviewer-approved-SHA + script-sha256 hash equality, remote
  database confirmation, strict-schema invariants,
  MISSING-after-READY hard-stop.
- **R4** — real-gcloud fixture parser tests, exact-shape
  rooms.status validators for pre/post/final snapshots,
  final-drift regressions (composite / override / duplicated
  target added after READY), and env-default finite-value
  enforcement.
- **R5** — full process-group drain on the SIGINT/SIGTERM trap
  (grandchild that installs `SIG_IGN` for SIGTERM is SIGKILLed
  before it can mutate; `trap-failure.txt` records survivors on
  quiescence failure), and env-var non-numeric values routed
  through rc=1 (usage) rather than rc=99 (internal).

Local run: fixture tests + static tests = **See the round's
tests transcript for the exact pass count** — regenerated each
round with `pytest -v` and delivered alongside the review
artifacts.

Run locally with:

```
pytest backend/tests/test_deploy_rooms_status_index.py \
       backend/tests/test_firestore_indexes_config.py -v
```

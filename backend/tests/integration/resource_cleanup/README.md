# Resource-cleanup integration harness

Two-process integration harness for the Track 1 resource-cleanup work
(audit F-15, and later F-8 / F-24 as those close in PR-T1-C).

Currently in scope: **F-15 only** — proving that starting a second
backend instance does not disrupt an active broadcast on the first.

## What runs where

- **Redis**: real, on `127.0.0.1:6379` by default. Two backends
  subscribe/publish through it.
- **Firestore emulator**: real emulator (needs JRE 21) on
  `127.0.0.1:8085` by default. Both backends read/write the same
  documents through it.
- **Deepgram stub**: a small in-test WebSocket server. The backend's
  STT client is pointed at it via `DEEPGRAM_ENDPOINT`. Accepts audio
  bytes, returns scripted `Results` frames. No paid Deepgram
  credentials are used or needed.
- **Backend A** and **Backend B**: two uvicorn subprocesses launched
  by the test, each on its own port, with distinct `INSTANCE_ID`,
  sharing the Redis and Firestore emulator above. Both run the real
  `app.main:app` entrypoint — no test-only routes, no diagnostic
  endpoints.

## Local dev

Prereqs on your machine:

```bash
# JRE 21 for the Firestore emulator
sudo apt install -y default-jre-headless   # or install temurin 21

# Firestore emulator via gcloud
gcloud components install cloud-firestore-emulator

# Python deps (already in requirements-dev.txt)
pip install -r backend/requirements-dev.txt
```

Then run the harness:

```bash
# Terminal 1 — Redis
docker run --rm -p 6379:6379 redis:7

# Terminal 2 — Firestore emulator
gcloud emulators firestore start \
    --host-port=127.0.0.1:8085 \
    --project=cleanup-track1-harness

# Terminal 3 — the tests
cd backend
pytest tests/integration/resource_cleanup/ -v
```

If either Redis or the emulator isn't reachable at collection time,
the whole suite skips with a clear message. CI's harness job fails
the run on any unexpected skip, so this cannot hide a broken harness
in green CI.

## Acceptance gate for F-15

`test_f15_starting_instance_b_does_not_disrupt_instance_a` passes
against current `main`. It fails deterministically against the
pre-PR-T1-A code — restore
`_cleanup_live_rooms_on_startup` and its
`asyncio.create_task(...)` schedule call, rerun this test, observe:

- backend_b's startup log line `Ending N stale live room(s)`
- listener_a receives a `STATUS(roomStatus=ended)` frame within a
  few seconds of B starting
- the room's `status` in Firestore is `ended`
- `test_f15_starting_instance_b_does_not_disrupt_instance_a` fails

That bisection is the "harness catches the actual defect" acceptance
criterion the reviewer required.

## Not in scope for this PR

- F-8 (missed terminal broadcast recovery via the reconciler).
- F-24 (Redis outage → cleanup → recovery).
- F-9 (SIGTERM close-code semantics).

Those land alongside PR-T1-C / PR-T1-D and will reuse this harness's
Deepgram stub + subprocess launcher + Firestore seeding.

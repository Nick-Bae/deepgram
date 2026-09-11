# Redis Pub/Sub — local 2-process smoke test

Verifies that translations broadcast from one backend instance reach listeners connected to a *different* backend instance, via Redis Pub/Sub. This is the core guarantee that makes `--max-instances > 1` on Cloud Run safe.

Design: `docs/02-design/features/redis-pubsub-fanout.design.md`

---

## Prerequisites

- Docker running locally
- Backend `venv` set up with `pip install -r requirements.txt` (which now includes `redis>=5.0.0,<6.0.0`)
- Two free ports for backend (default 8080/8081) and one for frontend (3000)

## 1. Start local Redis

```bash
docker run --rm -d --name worship-redis -p 6379:6379 redis:7
docker exec worship-redis redis-cli ping   # expect: PONG
```

## 2. Start two backend instances

Terminal A:
```bash
cd backend
REDIS_ENABLED=1 REDIS_HOST=127.0.0.1 REDIS_PORT=6379 \
  INSTANCE_ID=inst-A \
  venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080 --reload
```

Terminal B:
```bash
cd backend
REDIS_ENABLED=1 REDIS_HOST=127.0.0.1 REDIS_PORT=6379 \
  INSTANCE_ID=inst-B \
  venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8081 --reload
```

On each startup you should see:

```
[REDIS_PUBSUB] enabled connected=True instance=inst-A
```

## 3. Wire the frontend to two backends

Simplest: run the frontend twice, one pointing at each backend.

Terminal C (frontend against 8080 — will host the audio/producer):
```bash
cd frontend
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8080 \
NEXT_PUBLIC_WS_URL=ws://127.0.0.1:8080/ws/translate \
  npm run dev -- --port 3000
```

Terminal D (frontend against 8081 — listener view):
```bash
cd frontend
NEXT_PUBLIC_API_BASE_URL=http://127.0.0.1:8081 \
NEXT_PUBLIC_WS_URL=ws://127.0.0.1:8081/ws/translate \
  npm run dev -- --port 3001
```

## 4. Golden path

1. **Host** on port 3000 (backed by inst-A): open the host console, start a service, speak Korean.
2. **Listener A** on port 3000 (same instance as host): confirm translations appear.
3. **Listener B** on port 3001 (backed by inst-B, different instance): translations should appear identical to Listener A, with matching `seq` numbers.
4. **Monitor Redis** to confirm real cross-instance traffic:
   ```bash
   docker exec -it worship-redis redis-cli PSUBSCRIBE 'worshiptranslate:*'
   ```
   Each broadcast should appear once as a JSON envelope with `seq`, `publisher: "inst-A"`, `message: {...}`.

## 5. Failure modes to verify

- **Redis down mid-run:** `docker stop worship-redis`. Both instances log reconnect attempts. New translations stop crossing instances (host-instance listeners keep working via local fallback? No — under REDIS_ENABLED=1 we publish only. Design decision, see design doc §8.) Restart Redis: `docker start worship-redis` — subscribers automatically resubscribe every known room; cross-instance delivery resumes.
- **Second host on inst-B:** connect a host to port 3001 and speak. Listeners on inst-A should receive.
- **No duplicates:** each listener should receive each message exactly once. If you see doubles, something is calling `_broadcast_local_room` outside the subscriber callback.

## 6. Teardown

```bash
docker stop worship-redis
```

Backends fall back to local-only after they lose Redis; a clean stop of the uvicorn processes is enough.

---

## Interpretation

| Observation | Meaning |
|---|---|
| Listener B receives translations from host on inst-A | ✅ Redis Pub/Sub fanout working |
| Both listeners see identical `seq` values | ✅ Seq stamped at publish, delivered atomically |
| `PSUBSCRIBE` on Redis shows envelopes with `publisher: "inst-A"` | ✅ Envelope shape correct |
| Reconnect after `docker stop`/`start` | ✅ Reader loop backoff + resubscribe working |
| Listener B receives nothing | ❌ Backend didn't publish, or inst-B didn't subscribe; check startup logs for `[REDIS_PUBSUB] enabled connected=True` |
| Listener B receives duplicates | ❌ `broadcast_room` is delivering locally in addition to publishing; check `socket_manager.broadcast_room` |

# Redis Pub/Sub — Cloud Run + Memorystore runbook

**Goal:** turn on the Redis Pub/Sub fanout mechanism in production so cross-instance broadcast works, then run it for a month to see if it earns its keep. All state changes here are reversible — the "turn off" path is one env var.

**Prereqs:** PR #3 merged into `main` (or the `feature/redis-pubsub-fanout` branch code is on `main`).

Assumes standard `gcloud` CLI, logged in as an operator with `roles/redis.admin`, `roles/run.admin`, and `roles/compute.networkAdmin`.

Substitute your real values for these once at the top:

```bash
export PROJECT_ID=<your-gcp-project-id>
export REGION=us-central1
export SERVICE=<your-cloud-run-service-name>            # from the GH secret CLOUD_RUN_SERVICE
export VPC_NETWORK=default                              # or your VPC name
export VPC_SUBNET=default                               # subnet in $REGION
export REDIS_INSTANCE=worshiptranslate-redis
gcloud config set project "$PROJECT_ID"
```

---

## Cost

- **Memorystore Redis Basic tier, 1 GB, us-central1:** ~$35/month.
- **Direct VPC egress:** free (no VPC Connector needed).
- **Total added ops cost:** ~$35/month. Delete the Redis instance to stop the charge.

---

## Step 1 — Enable APIs

```bash
gcloud services enable redis.googleapis.com compute.googleapis.com
```

## Step 2 — Create the Memorystore Redis instance

Basic tier (single node, no replicas) is fine for the initial rollout. AUTH enabled so it doesn't answer to unauthenticated callers.

```bash
gcloud redis instances create "$REDIS_INSTANCE" \
  --size=1 \
  --region="$REGION" \
  --tier=basic \
  --network="projects/$PROJECT_ID/global/networks/$VPC_NETWORK" \
  --redis-version=redis_7_0 \
  --enable-auth
```

Takes ~5 minutes. When it finishes, grab the connection details:

```bash
export REDIS_HOST=$(gcloud redis instances describe "$REDIS_INSTANCE" \
  --region="$REGION" --format='value(host)')
export REDIS_AUTH=$(gcloud redis instances get-auth-string "$REDIS_INSTANCE" \
  --region="$REGION")
echo "REDIS_HOST=$REDIS_HOST"          # a 10.x.x.x address
# Do NOT echo REDIS_AUTH — treat as a secret from here on
```

## Step 3 — Store the auth string in Secret Manager

```bash
gcloud services enable secretmanager.googleapis.com

printf "%s" "$REDIS_AUTH" | gcloud secrets create redis-password --data-file=-

# Grant Cloud Run's runtime SA read access to the secret.
export RUN_SA=$(gcloud run services describe "$SERVICE" --region="$REGION" \
  --format='value(spec.template.spec.serviceAccountName)')
gcloud secrets add-iam-policy-binding redis-password \
  --member="serviceAccount:$RUN_SA" \
  --role=roles/secretmanager.secretAccessor
```

If `RUN_SA` is empty, Cloud Run is using the Compute default SA — grant it directly:

```bash
export PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
gcloud secrets add-iam-policy-binding redis-password \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor
```

## Step 4 — Attach Cloud Run to the VPC with Direct egress

Direct VPC egress lets Cloud Run reach the Memorystore private IP without a VPC Connector.

```bash
gcloud run services update "$SERVICE" \
  --region="$REGION" \
  --network="$VPC_NETWORK" \
  --subnet="$VPC_SUBNET" \
  --vpc-egress=private-ranges-only
```

## Step 5 — Set env vars and mount the secret

**Start with `--max-instances=1`** — we're smoke-testing Redis first, not scaling. Redis exercises the code path; scaling can wait a week.

```bash
gcloud run services update "$SERVICE" \
  --region="$REGION" \
  --max-instances=1 \
  --update-env-vars=REDIS_ENABLED=1,REDIS_HOST="$REDIS_HOST",REDIS_PORT=6379,REDIS_CHANNEL_PREFIX=worshiptranslate \
  --update-secrets=REDIS_PASSWORD=redis-password:latest
```

Cloud Run redeploys automatically after `services update`. Watch it:

```bash
gcloud run services describe "$SERVICE" --region="$REGION" \
  --format='value(status.latestReadyRevisionName,status.url)'
```

## Step 6 — Verify

Tail Cloud Run logs for the pubsub startup line:

```bash
gcloud run services logs read "$SERVICE" --region="$REGION" --limit=200 \
  | grep -E "REDIS_PUBSUB|\\[MULTICHURCH\\]|Uvicorn"
```

You want to see:

```
[REDIS_PUBSUB] enabled connected=True instance=inst-<random>
```

If instead you see `connected=False` or timeout errors, either the VPC egress isn't hitting Redis (Step 4) or the AUTH secret is wrong (Step 3).

Then run one worship service end-to-end and check that:
- Listeners see translations as before (no user-visible change vs pre-Redis).
- No `[REDIS_PUBSUB][warn]` lines in logs (those flag callsites broadcasting without org/room — a routing bug).

## Step 7 — Optional: bump `--max-instances` after a week of green

You said you want a month total; the safe cadence inside that month is:

- **Days 1-7:** `--max-instances=1`, `REDIS_ENABLED=1`. Redis exercised but not doing cross-instance work yet.
- **Days 8-21:** `--max-instances=2`. Now Redis is actually earning its keep. Watch a Sunday service.
- **Days 22-30:** `--max-instances=3` if Days 8-21 were clean, or hold at 2.

Update the deploy workflow to persist this — otherwise the next `main` merge overrides it:

```yaml
# .github/workflows/deploy-backend.yml
--max-instances=2   # or 3
```

## Turn OFF (any time, no code change)

```bash
gcloud run services update "$SERVICE" \
  --region="$REGION" \
  --update-env-vars=REDIS_ENABLED=0 \
  --max-instances=1
```

Optionally stop the Memorystore charge:

```bash
gcloud redis instances delete "$REDIS_INSTANCE" --region="$REGION"
```

Code stays deployed; `broadcast_room` reverts to local-only. Zero downtime, one env var.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Logs show `redis connect failed` at startup | VPC egress not hitting Redis private IP | Re-check Step 4; confirm `REDIS_HOST` matches `gcloud redis instances describe` |
| `AUTH failed` | Wrong secret value | Recreate `redis-password` secret from `gcloud redis instances get-auth-string` |
| `[REDIS_PUBSUB] enabled connected=False` and never recovers | Firewall blocking egress to 10.x.x.x:6379 | Add firewall rule allowing egress from Cloud Run subnet to Redis subnet on TCP/6379 |
| `[REDIS_PUBSUB][warn] broadcast_room called without org/room` | A callsite is broadcasting outside a room | Grep for that message type in `main.py` — likely needs `orgId`/`roomId` passed through |
| Listeners see stale/duplicate translations after reconnect | Frontend seq dedup working — this is *fine*, but if it's noisy in `d('ws', 'dropping duplicate fanout seq', ...)` logs during reconnects, that's evidence a replay buffer is worth building next |

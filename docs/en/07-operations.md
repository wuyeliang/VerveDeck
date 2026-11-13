# Operations Manual

## 1. Locations & Entry Points

| Item | Location |
|---|---|
| Source tree | `/opt/vervedeck/` (deploy/ images/ platform/ docs/) |
| Data root | `/data/` (models/datasets/tasks/uploads/GPU heartbeat/per-service data) |
| Object storage volume | Named volume `fintune_rustfs-data` (`docker volume inspect`) |
| Web console | `https://<ip>` (443 forces HTTPS, self-signed cert) |
| rustfs console | `http://<ip>:9001` (default rustfsadmin/rustfsadmin) |
| Inference API base | `https://<ip>/v1/<deploy_name>` (OpenAI-compatible, via web/backend reverse proxy, project API key auth) |

## 2. Daily Commands

```bash
cd /opt/vervedeck/deploy

# Status & logs
docker compose ps
docker compose logs -f --tail=100 backend      # backend (incl. scheduler 5s tick logs)
docker compose logs -f mineru                  # PDF parsing service
docker compose logs fintune-rustfs-1           # object storage

# Restart / upgrade
docker compose restart backend                 # restart backend
docker compose build backend && docker compose up -d backend   # after backend code changes
docker compose build web     && docker compose up -d web       # after frontend code changes
REBUILD=1 bash build-images.sh                 # rebuild task images (e.g. upgrade LLaMA-Factory/vLLM)
docker compose pull rustfs && docker compose up -d rustfs      # upgrade rustfs
```

## 3. Troubleshooting

### 3.1 Backend unhealthy / API 500

```bash
docker compose logs backend | tail -50
docker compose exec backend curl -sf http://127.0.0.1:3001/health
docker compose exec backend python -c "from app.services import storage; print(storage.health())"
# dependency checks
docker compose exec backend python -c "import psycopg2; print('pg ok')"
docker compose ps postgres rustfs opensearch   # are dependencies healthy
```

### 3.2 GPU task stuck in queued

- Check occupancy via `GET /api/cluster/queue`: a resident inference deployment
  may be holding the GPU → stop it or use a free GPU;
- The scheduler requires the node to be `online` with `gpu_count>0`:
  `GET /api/cluster/nodes`;
- Is the gpu-agent container writing heartbeats: `tail /data/gpu/agent.csv`,
  `cat /data/gpu/agent.ts`;
- Missing task image: `docker images | grep fintune`; if missing,
  `bash build-images.sh`.

### 3.3 Object storage issues

```bash
# rustfs process/logs
docker compose logs rustfs | tail -30
curl -s http://127.0.0.1:9000/health
# bucket & objects (inside the backend container)
docker compose exec backend python -c "
from app.services import storage
c = storage.client()
print([o.object_name for o in c.list_objects('vervedeck-datasets', recursive=True)][:20])"
# recreate the bucket (data will be lost — with care)
docker compose exec backend python -c "
from app.services import storage
c=storage.client(); c.make_bucket('vervedeck-datasets', exist_ok=True); print('ok')"
```

When S3 is unavailable the platform **does not stop**: dataset writes keep a
local cache only (the log carries an `s3 upload ... failed` warning); after
recovery, new writes sync automatically; historical versions can be re-uploaded
manually (`storage.put_dir`).

### 3.4 Training job failure

```bash
# failure reason (the DB keeps the last 30 log lines)
docker compose exec backend python -c "
from app.database import SessionLocal
from app.models import TrainingJob
db=SessionLocal()
for j in db.query(TrainingJob).filter(TrainingJob.status=='failed').order_by(TrainingJob.id.desc()).limit(3):
    print(j.id, j.name, (j.error or '')[:300])"
# full log
cat /data/jobs/<id>/run.log | tail -80
# config review
cat /data/jobs/<id>/config.yaml
# checkpoints still present → click "Resume" on the UI
ls /data/jobs/<id>/ckpt/
```

Common failures: OOM (lower the batch / enable zero3 / enable
gradient_checkpointing / lower cutoff_len);
invalid parameters (check the HF parser error on the first run.log line against
[04-training-parameters.md](04-training-parameters.md));
disk full (`df -h /data`, clean old job checkpoints: delete manually beyond
`save_total_limit`).

### 3.5 Running tasks after a backend restart

Within 5s the scheduler marks tasks whose exec context was lost as `failed`
(with a resume hint); checkpoints are preserved in `/data/jobs/<id>/ckpt/`, and
clicking "Resume" on the UI picks up where it left off.

## 4. Backup & Migration

**Must back up** (everything else can be rebuilt):
1. `/data/postgres` (or just `pg_dump`) — all business metadata;
2. the rustfs volume `fintune_rustfs-data` (primary store for dataset versions)
   — or `mc mirror` the bucket to a remote;
3. `/data/datasets` (local cache, optional; not needed once the S3 primary
   store is intact);
4. `/data/models/hub` (base weights; re-downloadable, but keeping them saves
   bandwidth);
5. `deploy/.env` (secrets).

```bash
# pg_dump
docker compose exec postgres pg_dump -U fintune fintune > backup_$(date +%F).sql
# rustfs bucket
docker run --rm -v fintune_rustfs-data:/data rustfs/rustfs:latest rustfs ...  # or mc mirror
# migration to a new machine: deploy per 02-one-click-deploy.md, then stop the
# stack → overwrite /data and the named volume → start the stack
```

## 5. Capacity & Cleanup

| Item | Recommendation |
|---|---|
| `/data/jobs/<id>/ckpt` | save_total_limit checkpoints per job; clean old jobs manually (keep merged-artifact paths) |
| `/data/gpu` (metrics) | the scheduler GCs GpuMetric older than 6h hourly — no intervention needed |
| DVC history | run `dvc gc` once `.git` grows (inside each dataset repo) |
| rustfs bucket | assess with `mc du`; deleting a dataset already clears its S3 prefix |
| HF cache | `/data/hf-cache` can be cleaned periodically (models already live in the hub dir) |

## 6. Security Baseline (production)

- Change `JWT_SECRET` / `ADMIN_PASSWORD` / `POSTGRES_PASSWORD` / rustfs
  credentials (edit `deploy/.env`, then `up -d` to rebuild);
- Do not expose ports 9000/9001 to the public internet (firewall); keep S3
  credentials least-privilege;
- API keys are project-scoped (`/api/admin/projects/{pid}/api-keys`); revoke
  immediately on suspected leakage;
- Audit log retention: archive the `AuditLog` table by `created_at` (the
  platform has no built-in cleanup — a DBA cron job).

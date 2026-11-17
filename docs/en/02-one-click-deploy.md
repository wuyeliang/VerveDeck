# One-Click Deployment Guide

## 1. Prerequisites

| Item | Requirement |
|---|---|
| OS | Linux x86_64 (verified on Ubuntu 20.04+ / Debian 11+) |
| Docker | ≥ 24.x + **docker compose v2 plugin** |
| GPU driver | NVIDIA driver (nvidia-smi available) |
| nvidia-container-toolkit | Installed (containers use `capabilities: [gpu]`) |
| Disk | ≥ 500GB recommended (model weights + datasets + images) |
| Network | Access to Docker Hub (or a domestic registry mirror configured) and HuggingFace (defaults to hf-mirror) |

> The platform also deploys without a GPU: CPU features (admin/datasets/DVC/Argilla)
> work normally; training/inference/MinerU are unavailable.

### 1.1 Base Dependencies on a Fresh Machine (one-time)

When Docker 24+ / compose v2 / NVIDIA driver / nvidia-container-toolkit are not
installed, run these one by one (verified on Ubuntu 24.04):

```bash
cat > /root/setup.sh << 'SH'
set -x
export DEBIAN_FRONTEND=noninteractive
dpkg --configure -a || true
apt-get install -f -y
apt-get update -y
apt-get install -y curl ca-certificates gnupg build-essential dkms linux-headers-$(uname -r)
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
VER=$(apt-cache search -n 'nvidia-driver-5[0-9][0-9]' | awk '{print $1}' | sed 's/^nvidia-driver-//' | sort -n | tail -1)
apt-get install -y "nvidia-driver-$VER"
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -y
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
systemctl enable --now docker
nvidia-smi
SH
bash /root/setup.sh
```

```bash
# If nvidia-smi is still unavailable (first driver install), reboot and verify again
nvidia-smi
```

> The repo's `deploy/setup-a3.sh` is a dependency-repair variant (assumes the
> Docker apt source is already configured, for resuming an interrupted install);
> the complete script is the inline version above.

### 1.2 Bare-Metal One-Click Install (install-from-iso.sh, recommended)

Right after an Ubuntu ISO install, one command does everything: `deploy/install-from-iso.sh` (all deployment scripts live in the repo's `deploy/` subdirectory).

```bash
# Get the script onto the machine (USB / scp / standalone clone), then:
sudo bash install-from-iso.sh
```

Four phases:

| Phase | What it does |
|---|---|
| 1 System deps | OS check (Ubuntu 20.04+/x86_64) → git/curl → Docker CE (Aliyun mirror) + compose v2 → NVIDIA driver (newest 5xx in apt) → nvidia-container-toolkit → **RDMA detection** (below) |
| 2 Code fetch | `git clone` into `/opt/vervedeck` (top-level project dir; existing repo → `git pull --ff-only`) |
| 3 Platform deploy | `cd deploy && bash deploy.sh` (.env, /data dirs, task-image builds, compose up, health check) |
| 4 Entry points | Platform URL https://<ip>, admin/admin123, common ops commands |

Optional environment variables:

| Var | Default | Meaning |
|---|---|---|
| `VD_REPO_URL` | `git@github.com:wuyeliang/VerveDeck.git` | Code repo (https URLs work too) |
| `VD_INSTALL_DIR` | `/opt/vervedeck` | Install directory (top-level project dir) |
| `VD_SKIP_DOCKER=1` | 0 | Skip Docker installation |
| `VD_SKIP_DRIVER=1` | 0 | Skip NVIDIA driver installation |

- **Idempotent**: safe to re-run — installed components are skipped, an existing clone is pulled. If a first-time driver install requires a reboot, reboot and re-run; dependencies are detected as already in place.
- **RDMA network**: Phase 1 probes `/dev/infiniband` or `lspci` for IB HCAs and, when found, installs `rdma-core` / `ibverbs-utils` / `infiniband-diags`. On hosts with IB/RoCE NICs, training/inference containers then automatically get HCA character devices (`/dev/infiniband/uverbs*`), `ipc=host`, and the host's libibverbs libraries mounted — NCCL multi-node training uses IB transparently. Single-node hosts without IB NICs are unaffected (nothing is injected). Cross-node RDMA also requires the nodes' RDMA network to be reachable (IB fabric or RoCE with PFC/ECN).

## 2. Getting the Code

```bash
# Source tree layout (directories needed for deployment)
vervedeck/
├── deploy/                  # compose + one-click scripts + .env.example
├── images/                  # 6 image build contexts
└── platform/
    ├── backend/             # FastAPI backend
    └── frontend/            # React frontend
```

Copy `vervedeck/` to the target machine (e.g. `/opt/vervedeck/`), then:

```bash
cd /opt/vervedeck/deploy
sudo bash deploy.sh
```

`deploy.sh` performs, in order:

1. **Environment pre-check**: docker / compose / nvidia-smi checks (missing GPU only warns)
2. **Generate .env**: on first run, copied from `.env.example` (with defaults)
3. **Create the /data directory structure**
4. **Build task images** (`build-images.sh`, idempotent: skipped if present, `REBUILD=1` forces rebuild)
5. **`docker compose up -d`** to bring up the full stack
6. **Health wait**: polls backend `/health` (up to 120s), confirms rustfs is healthy
7. Prints access URLs and default credentials

## 3. Service Stack (deploy/docker-compose.yaml)

| Service | Image | Port | Notes |
|---|---|---|---|
| web | fintune/web:1.0 | **3000**→80 | nginx: frontend statics + /api reverse proxy to backend |
| backend | fintune/backend:1.0 | 3001 (internal) | All FastAPI modules + scheduler; mounts `/data` + docker.sock |
| postgres | postgres:16-alpine | internal | Business DB fintune/fintune |
| redis | redis:7-alpine | internal | Argilla cache |
| opensearch | opensearch 2.17.1 | internal | Argilla 2.x mandatory search engine (single node, security plugin off) |
| argilla | argilla/argilla-server:v2.8.0 (official) | internal 3000 | Data annotation; auto-creates service account on startup (image user uid=1000; `chown 1000:1000 /data/argilla` required) |
| **rustfs** | rustfs/rustfs:latest | **9000** S3 / **9001** console | Object storage: primary store for dataset versions; named volume `rustfs-data` |
| mineru | fintune/mineru:1.0 | internal 8000 | PDF/document parsing (occupies 1 GPU) |
| gpu-agent | official nvidia/dcgm base | — | Heartbeat to `/data/gpu/agent.csv` every 5s (node registration/driver/CUDA/host metrics) |
| dcgm-exporter | official nvidia/dcgm-exporter | internal 9400 | GPU metrics → Prometheus |
| node-exporter | official prom/node-exporter | internal 9100 | CPU/memory/disk/network → Prometheus |
| prometheus | official prom/prometheus | internal 9091 | Metrics storage + alert evaluation (persisted in `/data/monitor`) |
| alertmanager | official prom/alertmanager | internal 9093 | Alert routing (WeCom/email) |
| grafana | official grafana/grafana | internal 3000 (embedded via web `/grafana/`) | Monitoring dashboards (anonymous read-only Viewer) |

**Task images** (not compose services; used by backend-created dynamic containers, lazy-loaded on demand):
`hiyouga/llamafactory:0.9.5` (official prebuilt), `vllm/vllm-openai:v0.33.0`, `lmsysorg/sglang:v0.5.23`, `modelscope-registry.cn-hangzhou.cr.aliyuncs.com/modelscope-repo/modelscope:ubuntu22.04-cuda12.4.0-py311-torch2.6.0-1.26.0-LLM` (official ModelScope image with evalscope built in; offline deployments can override `IMAGES_EVAL=fintune/evalscope:1.0` for the self-build fallback)

Dependency order: `postgres(healthy) → backend`; `rustfs(healthy) → backend`;
`opensearch(healthy) → argilla`.

## 4. Environment Variables (deploy/.env)

| Variable | Default | Notes |
|---|---|---|
| ADMIN_PASSWORD | admin123 | Initial platform admin password |
| JWT_SECRET | please-change-me… | **Must change in production** |
| POSTGRES_PASSWORD | fintune123 | Database password |
| RUSTFS_ACCESS_KEY / RUSTFS_SECRET_KEY | rustfsadmin | Object storage credentials |
| **S3_ENDPOINT** | http://rustfs:9000 | **Set empty `S3_ENDPOINT=` to disable object storage, pure local** |
| S3_BUCKET | vervedeck-datasets | Dataset bucket name (bucket auto-created by backend at startup, idempotent) |
| HF_ENDPOINT | https://hf-mirror.com | Model download mirror; use https://huggingface.co for direct access |
| ARGILLA_API_KEY | fintune-argilla-key | Annotation service API key |

## 5. Object Storage (rustfs)

- Dataset versions (`data.jsonl`/`raw.md`) are fully uploaded to the
  `vervedeck-datasets` bucket automatically after commit;
- Local `/data/datasets` is the processing cache: training symlink, MinerU
  parsing, and cleaning/synthesis need local paths;
  on a local miss it auto-downloads from rustfs (`dataset_service.ensure_version_local`);
- The bucket is auto-created by a backend background thread at startup
  (retries every 10s until rustfs is ready) — no manual bucket creation needed;
- Admin: `http://<ip>:9001` (rustfsadmin/rustfsadmin), or an S3 tool:
  ```bash
  mc alias set vervedeck http://<ip>:9000 rustfsadmin rustfsadmin
  mc ls vervedeck/vervedeck-datasets
  ```
- Degradation: with `S3_ENDPOINT` empty, all reads/writes go local and features
  are fully usable (dev/test mode).

## 6. Post-Deployment Verification

```bash
# 1) Service status
docker compose -f /opt/vervedeck/deploy/docker-compose.yaml ps

# 2) Backend health (web enforces HTTPS; add -k for the self-signed cert)
curl -sk https://127.0.0.1/health   # via web reverse proxy
# Or query the backend container directly:
docker compose exec backend curl -sf http://127.0.0.1:3001/health

# 3) Object storage
curl -s http://127.0.0.1:9000/health
docker compose exec backend python -c "from app.services import storage; print(storage.health())"

# 4) Login
# Open https://<ip> in a browser (443 forces HTTPS; on the self-signed cert
# warning click "Advanced → Continue")
# admin / admin123
```

## 7. Common Operations

```bash
cd /opt/vervedeck/deploy
docker compose ps                      # status
docker compose logs -f backend         # backend logs (incl. scheduler)
docker compose restart backend         # restart backend (scheduler self-heals: running tasks marked failed, resume hint)
docker compose pull rustfs && docker compose up -d rustfs   # upgrade rustfs
REBUILD=1 bash build-images.sh         # rebuild all task images
docker compose down                    # stop stack (volumes kept; /data and named volumes not removed)
```

> For detailed operations see [07-operations.md](07-operations.md).

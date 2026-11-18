# VerveDeck Technical Whitepaper

**Version**: 1.0 (2026-11) · **Companion docs**: [Architecture](01-architecture.md) · [API Reference](05-api-reference.md) · [Deployment Guide](02-one-click-deploy.md)

> 中文版本：[../10-技术白皮书.md](../10-技术白皮书.md)

## 1. Product Positioning

VerveDeck is an **open-source, one-stop LLM fine-tuning, training & inference platform** for single-machine or multi-node GPU clusters, positioned around four capability domains — hardware resource management, dataset management, fine-tuning/training, and model inference — covering the full loop "data → training → evaluation → compression → deployment → operations", served to clients through an OpenAI-compatible API.

Design goals:

1. **Usable on a single machine**: one GPU server + one deploy command runs the entire pipeline;
2. **Scalable to multi-node**: register-based node scaling; training supports torchrun multi-node/multi-GPU (DDP/DeepSpeed/FSDP); serving supports multi-replica load balancing;
3. **All open-source engines**: serving with vLLM/SGLang, training with LLaMA-Factory, monitoring with DCGM + Prometheus + Grafana, object storage S3-compatible (built-in rustfs, MinIO-compatible);
4. **Private & controllable**: fully containerized, data stays on-prem, RBAC + audit + guardrails for enterprise compliance.

## 2. Overall Architecture

```
                    ┌────────────────────────────────────────────┐
 Browser ─HTTPS:443─▶│ web (nginx)                                │
                    │  ├─ /            frontend SPA (React+AntD) │
                    │  ├─ /api/*       → backend:3001            │
                    │  ├─ /v1/<deploy>/* → backend OpenAI proxy  │
                    │  └─ /grafana/*   → grafana (iframe embed)  │
                    └────────────────────────────────────────────┘
                                        │
                    ┌───────────────────▼────────────────────────┐
                    │ backend (FastAPI)                          │
                    │  ├─ REST API (13 modules, ~110 endpoints)  │
                    │  ├─ OpenAI-compatible proxy (auth/guard)   │
                    │  ├─ Job scheduler (queue/GPU assign)       │
                    │  └─ Container orchestration (docker sock)  │
                    └───┬──────────┬──────────┬──────────┬───────┘
                        │          │          │          │
              ┌─────────▼──┐ ┌─────▼─────┐ ┌──▼──────┐ ┌─▼────────────┐
              │ postgres   │ │ redis     │ │ rustfs  │ │ Task pool     │
              │ (metadata) │ │ (cache)   │ │ (S3)    │ │ train/serve/  │
              └────────────┘ └───────────┘ └─────────┘ │ quantize ctrs │
                        │                              └──────────────┘
        ┌───────────────┼────────────────────────┐
  ┌─────▼─────┐  ┌──────▼──────┐  ┌──────────────▼──┐
  │ gpu-agent │  │ node-exporter│ │ dcgm-exporter   │──▶ Prometheus ──▶ Grafana/Alertmanager
  │ (heartbeat│  │ (CPU/mem/disk│ │ (GPU metrics)   │      ▲
  │ /host)    │  └─────────────┘  └─────────────────┘      │
  └───────────┘                                vllm/sglang/llamafactory native /metrics ┘
```

**Key component responsibilities**:

| Component | Image | Responsibility |
|---|---|---|
| web | self-built (nginx) | static assets + 443 TLS termination + reverse proxy |
| backend | self-built (FastAPI) | all business APIs, scheduler, container orchestration |
| postgres | postgres:16 | metadata (tenants/projects/assets/jobs/audit) |
| redis | redis | lightweight cache |
| rustfs | S3-compatible | dataset/model object storage (swappable with any S3) |
| mineru | self-built | PDF/Office parsing |
| argilla | official | data annotation |
| gpu-agent | nvidia/dcgm + mounted scripts | node heartbeat, driver/CUDA versions, host metrics |
| node-exporter / dcgm-exporter | official | Prometheus metric collection |
| prometheus / alertmanager / grafana | official | metrics storage, alert routing, visualization |
| task containers | vllm / sglang / llamafactory | training/serving/quantization workloads (created on demand) |

## 3. Core Mechanisms

### 3.1 Task Containers & Pause-Based Scene Preservation

Training/quantization jobs run in dedicated containers ("pause task-container mechanism"):

- **Cancel ≠ kill**: cancel `docker pause`s the container — the process is frozen with GPU memory and on-disk scene fully preserved;
- **Resume is continue**: `docker unpause` + framework checkpointing continues the job; queue-based resource semantics back it up;
- **Delete destroys**: deleting the job removes the container, with **artifact protection** — artifacts already registered in the model repository (merged models / quantized artifacts) are never removed as a side effect.

This makes "pause-inspect-resume" a first-class operation for long training runs, not a one-shot destructive cancel.

### 3.2 Engine Image Lazy Loading

The platform never pre-pulls engine images at deploy/startup (vLLM alone is 35GB+):

1. On job/deployment creation, check the local image;
2. If missing, pull in the background (official images via `docker pull`; self-built images streamed `docker build` from the host's read-only mounted `/repo` source tree);
3. The scheduler polls image state each round and creates the container only when ready;
4. The images page shows pull/build progress and supports forced refresh.

Result: lightweight platform install (seconds), engines paid on demand (time/bandwidth), and image versions hot-swappable via the cluster-upgrade runtime pin.

### 3.3 Model Lineage

All model assets (base / LoRA / merged / quantized) live on a global lineage graph:

- On training completion: the LoRA is registered, `parent` = base model;
- On merge/quantize completion: the artifact is registered, `parent` = source asset, with canonical naming (`quant-4bit-*` etc.);
- The lineage API returns the full chain, visualized in the UI;
- Delete protection follows lineage (referenced assets are undeletable; quantized artifacts survive task deletion).

### 3.4 LoRA Hot-Loading

Serving deployments support attaching/detaching LoRA adapters without restart (vLLM `/v1/load_lora_adapter` semantics):

- Multiple LoRAs on one base = one base serving many finetunes, with shared memory;
- Unload requires confirmation (requests targeting the adapter fail immediately);
- Integrated with the OpenAI proxy: the request `model` field selects the adapter.

### 3.5 Multi-Node Multi-GPU Distributed Training

- Register-based nodes: each machine's gpu-agent heartbeats (GPU list, driver, CUDA, host metrics); the backend maintains `gpu_nodes`;
- A job declares "nodes × GPUs-per-node"; the scheduler allocates across nodes;
- torchrun launches multi-process inside the container network, supporting native DDP / DeepSpeed ZeRO / FSDP;
- In IB/RDMA environments, NCCL parameters are configurable (NCCL_IB_* passed through job advanced params);
- Automatic RDMA device injection: at container creation, host `/dev/infiniband` character devices (uverbs*/rdma_cm), `ipc=host`, and a read-only host libibverbs mount are injected automatically, so NCCL discovers the HCAs and uses IB; hosts without IB NICs are unaffected. The one-click installer auto-installs the rdma-core userspace stack when an IB HCA is detected.

### 3.6 OpenAI-Compatible Proxy & Guardrails

`/v1/{deploy_name}/*` as the single entry point:

- **Auth**: project API Key (Bearer), optionally scoped to a model set;
- **Forwarding**: request `model` normalized via the deployment's `served_model_name` mapping; streaming SSE passed through;
- **Guardrails**: input/output matched against guard rules (regex/keyword, project-scoped); hits return 451;
- **Observability**: every call lands in the audit log (401/403 denials included).

### 3.7 Monitoring & Alerting

Three collection layers:

1. **Node layer**: gpu-agent heartbeat (node liveness/driver/CUDA/host metrics) + node-exporter (CPU/memory/disk/network) + dcgm-exporter (GPU utilization/memory/temp/power/ECC/Xid/throttling/Remap);
2. **Workload layer**: native `/metrics` of vLLM/SGLang/LLaMA-Factory scraped directly by Prometheus (serving QPS/latency/KV cache; training loss/eval);
3. **Alerting layer**: platform-managed Prometheus alert rules (DB-backed, UI CRUD, dry-run), routed by Alertmanager to WeCom/email (channels DB-configurable).

Rule capacity is 5000 (requirement ≥2000): the list API is server-side paginated with category/type/state/name filters; rules carry a free-form category, and the rendered Prometheus rule file is **split into rule groups per category** (serial within a group, parallel across groups) to avoid long-tail evaluation on large rule sets — uncategorized rules keep the legacy group name so existing routing is untouched. Alert events support manual acknowledge (ack, recording acker + time) and deletion, both single and bulk (≤1000 per call).

Grafana is embedded in the platform monitoring dashboard via iframe (`GF_SECURITY_ALLOW_EMBEDDING=true` + `GF_SERVER_ROOT_URL` sub-path, anonymous read-only Viewer); time range and dashboard selection are platform-controlled.

### 3.8 Cluster Upgrade (Blue-Green)

- **Engine kinds** (vllm/sglang/llamafactory/gpu-agent): Docker Hub version probe (10min cache) → pull new tag → write runtime pin (`/data/upgrade/image_pins.json`); **running jobs are untouched**, new jobs use the new image;
- **Self-built task image kinds** (evalscope evaluation / mineru document parsing): rebuild the same tag from the host `/repo` source tree (a rebuild upgrades its dependencies); evalscope only affects new eval jobs, MinerU's resident container is blue-green recreated (seconds of interruption);
- **Platform kinds** (backend/web): rebuild image from host `/repo` + blue-green container recreate (temp-named new container → verify running → stop old, remove old, rename; service aliases keep DNS stable);
- **Self-upgrade convergence**: when backend upgrades itself, the old process dies with its container; the new process's scheduler `converge()` checks the temp container each round and completes the switch (15min timeout → failed).

### 3.9 Security Model

- **4-level RBAC**: super_admin / tenant_admin / developer / viewer, with tenant-project two-level isolation;
- **Auth**: JWT (72h default) + forced first-login password change + failure lockout (threshold/duration configurable) + IP whitelist;
- **Audit**: all write ops + 401/403 denials recorded, filterable by action/user/status;
- **Transport**: 443 HTTPS enforced (TLS terminated at web);
- **Content safety**: guardrail rules intercept serving input/output;
- **Keys**: API Keys issued per project, model-scoped, batch-revocable.

## 4. Data Directories & Persistence

| Directory | Contents |
|---|---|
| /data/tenants, /data/projects | tenant/project work dirs |
| /data/models | model asset files |
| /data/datasets | dataset files (DVC repos) |
| /data/upgrade | runtime image pins |
| /data/monitor | Prometheus/Alertmanager state |
| rustfs buckets | object storage data (S3) |
| postgres volume | all metadata |

Backup = postgres logical dump + /data + rustfs buckets; together they are the entire state.

## 5. Deployment Topologies

| Topology | Notes |
|---|---|
| Single machine, 1 GPU | minimal form; one `deploy.sh` brings up the whole stack (~10 containers) |
| Single machine, multi-GPU | jobs allocated per GPU; multi-replica serving |
| Multi-node | each machine runs gpu-agent + a task pool, registered into the cluster; cross-node torchrun training |
| External object storage | replace rustfs with MinIO/COS/OSS or any S3 (storage page backend config) |

Engine versions are all changeable online via System → Cluster Upgrade; no platform reinstall needed.

## 6. Technology Choices (Summary)

| Choice | Rationale |
|---|---|
| FastAPI + SQLAlchemy + PostgreSQL | async API, typed, enterprise-grade metadata |
| React + Ant Design | component density for tables/forms; 6-language i18n |
| Native Docker orchestration (not K8s) | in single-machine private deployments K8s ops cost is too high; container-as-job makes pause semantics direct |
| DVC + Argilla | mature open-source dataset versioning & annotation; avoid re-inventing |
| Prometheus stack | de-facto standard metrics stack, native vLLM/DCGM ecosystem fit |
| Image lazy loading + runtime pin | unified solution for lightweight install and hot engine upgrade |

## 7. Roadmap Directions

- Serving P/D (prefill/decode) disaggregation & pooling for long-context workloads
- Extended quantization methods (FP8/FP4, following engine and hardware generations)
- Cross-cluster federated job scheduling
- Cost accounting views (GPU-hours × project allocation)

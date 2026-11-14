# VerveDeck Architecture

## 1. System Overview

VerveDeck is a containerized LLM fine-tuning platform covering the full pipeline
**Data → Training → Evaluation → Deployment → API Serving**,
closed-loop on a single machine (one GPU server), with 7 functional modules:

| Module | Responsibility | Main Components |
|---|---|---|
| Project Management | Tenants/Projects/Members/RBAC/Audit | Postgres (SQLAlchemy ORM) |
| Data Center | Dataset versions, parsing, cleaning, synthesis, annotation | rustfs (S3 primary store) + MinerU + Argilla |
| Model Repository | Base model download, LoRA/merged artifacts, lineage | HuggingFace Hub + DVC (local metadata) |
| Training Center | CPT/SFT/DPO/merge, LoRA/Full/Freeze, DeepSpeed | LLaMA-Factory (task containers) |
| Evaluation Center | Rule matching / LLM-Judge / evalscope benchmarks | evalscope (task containers) |
| Inference Deployment | vLLM/SGLang multi-replica, hot LoRA adapter loading | vLLM/SGLang (task containers) |
| Cluster & Monitoring | GPU heartbeat, queue scheduling, utilization, Dashboard | gpu-agent + scheduler thread |

```
                        ┌────────────────────────────────────────────┐
                        │   web (nginx https :443, port 80 = 301)     │
                        └───────────────────────┬────────────────────┘
                                                │ /api reverse proxy
                        ┌───────────────────────▼────────────────────┐
                        │   backend (FastAPI :3001, internal net)     │
                        │  ┌─────────────┐  ┌──────────────────────┐ │
                        │  │ REST API    │  │ Scheduler thread     │ │
                        │  │ RBAC+Audit  │  │ (5s poll: heartbeat/ │ │
                        │  └─────────────┘  │ admission/converge/  │ │
                        │  /data volume     │ model download)      │ │
                        │  docker.sock      └──────────┬───────────┘ │
                        └──────┬──────────────────────┬─┼────────────┘
              ┌────────────────┼────────────┬─────────┼──────────┬───────────┐
              ▼                ▼            ▼         ▼          ▼           ▼
        ┌──────────┐   ┌────────────┐ ┌─────────┐ ┌────────┐ ┌────────┐ ┌─────────┐
        │ postgres │   │ redis      │ │opensearch│ │ argilla│ │ rustfs │ │mineru   │
        └──────────┘   └────────────┘ └──────────┘ └────────┘ └────────┘ └─────────┘
                        │
              ┌─────────▼─────────────────────────────────────────┐
              │   Task container pool (pause placeholder + exec)  │
              │  ft-train-{job.id}   llama-factory / deepspeed    │
              │  ft-eval-{task.id}   evalscope + runner           │
              │  ft-infer-{dep.id}   vllm / sglang                │
              │  GPUs bound exclusively at container creation     │
              └───────────────────────────────────────────────────┘
```

## 2. Key Design Decisions

### 2.1 Task Container = pause placeholder + docker exec

All GPU tasks (training/eval/inference) are **not** statically orchestrated by
compose; the backend creates them dynamically via the Docker API:

1. **Create a placeholder container** (`taskcontainer.create_container`):
   - The image is the real task image (e.g. `hiyouga/llamafactory:0.9.5`) but with
     `command=["sleep","infinity"]` — the main process only "holds" the GPU;
   - **GPUs are bound exclusively at creation time**:
     `HostConfig.DeviceRequest(count=gpu_count, capabilities=["gpu"])` —
     nvidia-container-toolkit injects the physical GPUs into the container,
     giving natural isolation;
   - Container naming: `ft-train-{job.id}` / `ft-eval-{task_id}` / `ft-infer-{dep.id}(-r{replica})`;
   - `/data` is mounted (task dirs/datasets/models all visible on disk).
2. **exec starts the real process** (`start_task`): the backend spawns a daemon
   thread that runs `docker exec` (e.g. `llamafactory-cli train /data/jobs/1/config.yaml`),
   streaming stdout to `run.log` in real time; the exit code is recorded in the
   `RUNNERS` registry.
3. **Scheduler polls for convergence**: every 5s it reads exit codes from
   `RUNNERS` → success/failure → register artifact / collect results →
   `remove_container`.
4. **Cancel**: `pkill` the training process inside the container + force-remove
   the container; checkpoints stay in `/data/jobs/<id>/ckpt` for resume.

Benefits:
- GPU capacity = a hard constraint at container creation; the scheduler only
  does "admission queuing" (free = node GPU count − in-use);
- Self-healing after backend restart: if the scheduler finds a running task
  whose exec context is lost, it marks it failed and points to "resume from
  checkpoint" (checkpoints are preserved);
- Container lifecycle is 1:1 with the task; logs/config/tensorboard all live in
  the host's `/data/jobs/<id>/`, so removing the container loses nothing.

### 2.2 Scheduler (backend background thread, 5s tick)

| Step | Logic |
|---|---|
| GPU heartbeat | The gpu-agent container writes `/data/gpu/agent.csv` every 5s; `gpu_service.tick()` ingests it into `GpuMetric`; no heartbeat for >60s = offline |
| State convergence | Training/Eval: read exec exit code → succeeded/failed; Inference: container liveness check + creating→running |
| Admission | `free = node.gpu_count − (running training gpu_count + running deployments tp×replicas)`; admit by `priority desc, id asc` |
| Model download | `pending_download` assets are downloaded inside a backend thread via `snapshot_download` (no container); status → `ready` on completion |
| Metric GC | Purge `GpuMetric` rows older than 6h, hourly |

**GPU mutual exclusion**: the same physical GPU can never be bound by two task
containers simultaneously — the binding happens at container creation (exclusive),
so the scheduler's capacity check is just a "soft ledger" for queue admission;
hard isolation is guaranteed by the nvidia runtime.

### 2.3 Datasets = versioned + object storage

- Local repo: `/data/datasets/<project>/<name>/`, DVC manages version metadata
  (local remote);
- **Primary store: rustfs (S3-compatible)**: after each version commit,
  `storage.put_dir` fully syncs the version to the `vervedeck-datasets` bucket;
  key = local path relative to `/data`;
- All read paths go through `dataset_service.ensure_version_local`: local cache
  hit → use directly (training symlink / cleaning / synthesis / eval all need
  local paths); miss → automatic `ensure_local` download from rustfs;
- When `S3_ENDPOINT` is empty, every call site **degrades silently** to pure
  local storage (dev/test environments);
- Deleting a dataset: local dir + DVC metadata first, then `storage.delete_dir`
  for the S3 prefix.

### 2.4 Permissions & Audit

- JWT auth; roles `admin / project_admin / member / viewer`, project-level RBAC;
- All write operations pass through middleware that records `AuditLog`
  (including 401/403 rejections);
- Guard rules (GuardRule): regex hits on input/output are intercepted, with
  project-level or global scope.

## 3. Data Directory Layout (host `/data`)

```
/data
├── models/hub/<name>/        # base models (HF download)
├── datasets/<proj>/<name>/   # dataset local repo (cache of rustfs primary store)
│   └── versions/v<n>.jsonl
├── jobs/<job.id>/            # training job: config.yaml / dataset_info.json / run.log / ckpt/ / runs/
├── evals/<task.id>/          # eval task: task.json / test.jsonl / run.log / result.json
├── uploads/                  # uploaded files (MinerU parsing)
├── gpu/agent.csv agent.ts    # GPU heartbeat
├── argilla/ postgres/ redis/ opensearch/   # per-service data
└── dvc-remote/               # DVC local remote
```

## 4. Image List

| Image | Build Context | Purpose |
|---|---|---|
| `fintune/backend:1.0` | platform/backend | FastAPI platform backend (compose service) |
| `fintune/web:1.0` | platform/frontend | React + AntD (nginx, compose service) |
| `fintune/argilla:1.0` | images/argilla | Data annotation (compose service) |
| `fintune/mineru:1.0` | images/mineru | PDF parsing (compose service, 1 GPU) |
| `hiyouga/llamafactory:0.9.5` | official (hiyouga) | **Task image**: LLaMA-Factory 0.9.5 official prebuilt (CUDA 12.4 + torch); offline fallback self-build in images/llama-factory |
| `vllm/vllm-openai:v0.33.0` | official | **Task image**: vLLM inference engine (CUDA 13.5 built-in) |
| `lmsysorg/sglang:v0.5.23` | official | **Task image**: SGLang inference engine (CUDA 13.0 built-in) |
| `fintune/evalscope:1.0` | images/evalscope | **Task image**: eval engine + runner.py |
| `nvidia/dcgm:4.7.1-ubuntu24.04` | official (NVIDIA) | GPU monitoring (open-source image + mounted collect.sh, zero local build) |
| `rustfs/rustfs:latest` | official | S3 object storage (compose service) |

The task images (llamafactory/vllm/sglang/evalscope) are not compose services;
they are loaded lazily on demand (official images pulled, evalscope self-built).

# API Reference

Base URL: `https://<ip>/api/...` (443 forces HTTPS via the web reverse proxy;
inside the container network it is `:3001`; add `-k` to curl for the
self-signed cert)
Auth: `Authorization: Bearer <token>` (obtained from `POST /api/auth/login`,
valid for 72h by default)

## Auth /api/auth

| Method | Path | Description |
|---|---|---|
| POST | /api/auth/login | `{username, password}` → `{token, user}` |
| GET | /api/auth/me | Current user info |
| PUT | /api/auth/password | Change password `{old_password, new_password}` |

## Admin /api/admin (admin or project owner)

| Method | Path | Description |
|---|---|---|
| GET/POST | /api/admin/tenants | List tenants / create |
| GET/POST | /api/admin/projects | List projects / create `{tenant_id, name}` |
| PUT | /api/admin/projects/{pid}/members | Update member `{user_id, role}` |
| DELETE | /api/admin/projects/{pid}/members/{uid} | Remove member |
| GET/POST | /api/admin/users | List users / create |
| PUT | /api/admin/users/{uid} | Modify user (role / password reset) |
| GET/POST/DELETE | /api/admin/projects/{pid}/api-keys[/{kid}] | Project API keys (auth for the OpenAI-compatible proxy) |
| GET/POST/DELETE | /api/admin/guard-rules[/{rid}] | Guardrail rules |
| GET | /api/admin/audit | Audit log (paginated/filterable) |

## Datasets /api/datasets

| Method | Path | Description |
|---|---|---|
| GET | /api/datasets?project_id= | List |
| POST | /api/datasets | Create `{project_id, name, description, kind?, scenario?}` (initializes the DVC repo) |
| DELETE | /api/datasets/{id} | Delete (local + DVC + **S3 prefix**) |
| GET | /api/datasets/{id} | Details |
| GET | /api/datasets/{id}/versions | Version list |
| POST | /api/datasets/{id}/versions/upload | Upload JSONL/JSON/CSV to create a new version (multipart `file`, `fmt`) |
| POST | /api/datasets/{id}/versions/{vid}/commit | DVC commit (`v{N}` lands in DVC rev, synced to S3) |
| GET | /api/datasets/{id}/versions/{vid}/samples?page&size | Sample preview (auto-fetched from S3) |
| GET | /api/datasets/{id}/versions/{vid}/raw | Original document (parsing artifact) |
| GET | /api/datasets/{id}/dvc-log | DVC commit history |
| POST | /api/datasets/parse | Document parsing (MinerU, multipart) → new version |
| POST | /api/datasets/clean | Cleaning task (backend thread) → new version |
| POST | /api/datasets/synth | Synthesis task (QA/CoT, calls an inference deployment) → new version |
| POST | /api/datasets/{id}/annotate | Push to Argilla for annotation |
| GET | /api/datasets/tasks | Data task queue (parse/clean/synth status polling) |

## Models /api/models

| Method | Path | Description |
|---|---|---|
| GET | /api/models?project_id= | Asset list (base/lora/merged + status; live download size for pending) |
| GET | /api/models/search?source=hf\|ms&q= | Model search (backend proxy for HuggingFace / ModelScope) |
| POST | /api/models/base | Register a base model `{project_id, name, hf_id}` (prefix `ms:` for ModelScope) → background download |
| POST | /api/models/upload | Local upload: zip of a full model dir (multipart) |
| POST | /api/models | Register an existing local asset `{path, type}` |
| GET | /api/models/{id} | Details |
| GET | /api/models/{id}/lineage | Lineage chain |
| DELETE | /api/models/{id} | Delete |

## Training /api/training

| Method | Path | Description |
|---|---|---|
| POST | /api/training/jobs | Create job `{project_id, name, stage(cpt/sft/dpo/export), finetuning_type(lora/full/freeze), base_model_id, dataset_version_ids[], hyperparams{}, deepspeed(none/zero2/zero3), gpu_count, priority, adapter_asset_id?}` |
| GET | /api/training/jobs?project_id&status | List |
| GET | /api/training/jobs/{id} | Details (incl. checkpoints, container_name) |
| GET | /api/training/jobs/{id}/metrics | tensorboard metrics (loss/lr/epoch series) |
| GET | /api/training/jobs/{id}/logs?tail= | Tail of the run log |
| POST | /api/training/jobs/{id}/cancel | Cancel (kill process + remove container, checkpoints kept) |
| POST | /api/training/jobs/{id}/resume | Resume from checkpoint (creates a new job; empty `checkpoint`=latest) |
| GET | /api/training/params | **Full parameter surface (10 groups, 94 params; drives the UI form)** |
| GET | /api/training/templates | Stage hyperparameter templates |

## Evaluation /api/evals

| Method | Path | Description |
|---|---|---|
| POST | /api/evals | Create `{project_id, dataset_version_id, target_deployment_id / target_asset_id, method(rule/judge/both/benchmark), judge_deployment_id?, benchmark?, limit?}` |
| GET | /api/evals | List |
| GET | /api/evals/{id} | Details (summary + details) |
| GET | /api/evals/{id}/report | Download the evaluation report (Markdown) |
| POST | /api/evals/compare | Multi-task comparison |
| DELETE | /api/evals/{id} | Delete |

## Inference /api/inference/deployments

| Method | Path | Description |
|---|---|---|
| POST | /api/inference/deployments | Create `{project_id, name, model_asset_id, engine(vllm/sglang), quantization, tensor_parallel, max_model_len, gpu_mem_util, replicas, enable_lora?, lora_adapters?, description?}` |
| GET | /api/inference/deployments | List |
| GET | /api/inference/deployments/{id} | Details (endpoint / containers / status) |
| POST | /api/inference/deployments/{id}/start | Start |
| POST | /api/inference/deployments/{id}/stop | Stop |
| POST | /api/inference/deployments/{id}/scale | Scale replicas `{replicas}` |
| GET | /api/inference/deployments/{id}/logs | Logs |
| POST | /api/inference/deployments/{id}/adapters | Mount LoRA `{name, asset_id}` (vLLM hot-load) |
| DELETE | /api/inference/deployments/{id}/adapters/{name} | Unload LoRA |
| DELETE | /api/inference/deployments/{id} | Delete deployment |

Once a deployment is ready it exposes an **OpenAI-compatible endpoint**
(`https://<ip>/v1/<deploy_name>/chat/completions`), authenticated with a project
API key; guardrails apply to the output side.

## Model Compression /api/quantize

| Method | Path | Description |
|---|---|---|
| GET | /api/quantize/methods | Supported quantization methods (bnb4/bnb8/gptq4/awq4) |
| GET | /api/quantize?project_id= | Quantization task list |
| POST | /api/quantize | Create task `{project_id, name, model_asset_id, method}` |
| GET | /api/quantize/{id} | Details |
| GET | /api/quantize/{id}/logs?tail= | Run log tail |
| DELETE | /api/quantize/{id} | Delete (terminal-state tasks only) |

Quantized artifacts are auto-registered in the model repository as `type=merged`
and can be deployed directly.

## Cluster /api/cluster

| Method | Path | Description |
|---|---|---|
| GET | /api/cluster/nodes | GPU nodes (heartbeat / online status) |
| GET | /api/cluster/nodes/{id}/metrics | Historical utilization curves |
| GET | /api/cluster/queue | Queued + running tasks |
| PUT | /api/cluster/queue/{jid} | Adjust priority `{priority}` |
| POST | /api/cluster/queue/{jid}/promote | Move to front of queue |
| GET | /api/cluster/utilization | Current GPU occupancy breakdown |

## Dashboard /api/dashboard

| Method | Path | Description |
|---|---|---|
| GET | /api/dashboard/summary | Aggregates: dataset/model/task/eval/deployment counts, GPU status, **object storage health (storage)**, recent tasks/evals/deployments/audit |

## General Conventions

- Errors: HTTP 4xx/5xx + `{"detail": "..."}`; both 401/403 are written to the
  audit log;
- List pagination: most endpoints take `?page&size` or `limit` (see the
  specific implementation);
- Task-entity state machine: `queued → running → succeeded / failed / cancelled`;
- All write operations require the corresponding project role:
  `owner/admin/developer` (viewer is read-only).

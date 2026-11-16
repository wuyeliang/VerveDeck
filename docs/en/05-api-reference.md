# API Reference

Complete REST API route table of the VerveDeck platform backend, one-to-one with `platform/backend/app/api/*.py` (13 modules, ~110 endpoints).

## General Conventions

- **Base URL**: `https://<host>:443/api/...` (443 forces HTTPS via the web reverse proxy; inside the container network it is `backend:3001`; add `-k` to curl for the self-signed cert)
- **Auth**: `Authorization: Bearer <token>`. Obtained from `POST /api/auth/login`, valid for 72h by default (adjustable via login config); brute-force protection: user lockout after too many failures
- **Request/Response**: JSON (file uploads use multipart/form-data)
- **Error format**: `{"detail": "message"}` with HTTP status 400/401/403/404/409/422/502/503
- **RBAC**: roles super_admin > tenant_admin > developer > viewer; endpoints requiring a higher role return 403
- **Batch delete unified response** (all `/batch-delete` endpoints):

  ```json
  {"deleted": [1, 2], "failed": [{"id": 3, "error": "referenced by a training job"}]}
  ```

  Executes the single-delete logic in a loop; one failure does not block the rest; referenced/non-existent items go to `failed` with a reason.
- **Audit**: all write operations and 401/403 denials are recorded in the audit log (queryable via `/api/admin/audit`)

## Auth /api/auth

| Method | Path | Description |
|---|---|---|
| POST | /api/auth/login | `{username, password}` → `{token, user}`; failures counted, lockout after threshold |
| GET | /api/auth/me | Current user info |
| PUT | /api/auth/password | Change password `{old_password, new_password}` (forced on first login) |

## Tenants & Projects /api/admin (super_admin; project owner may manage own project)

| Method | Path | Description |
|---|---|---|
| GET/POST | /api/admin/tenants | List / create tenant `{name}` |
| GET/POST | /api/admin/projects | List projects (incl. member user-id list) / create `{tenant_id, name}` |
| PUT | /api/admin/projects/{pid}/members | Update member `{user_id, role}` |
| DELETE | /api/admin/projects/{pid}/members/{uid} | Remove member |
| GET/POST | /api/admin/users | List / create user `{username, password, role}` |
| PUT | /api/admin/users/{uid} | Modify user (role / password reset) |

### API Keys (OpenAI-compatible proxy auth, model-scoped)

| Method | Path | Description |
|---|---|---|
| GET/POST | /api/admin/projects/{pid}/api-keys | List / create `{name, models?}` (empty models = unrestricted) |
| DELETE | /api/admin/projects/{pid}/api-keys/{kid} | Delete (sets is_active=false) |
| POST | /api/admin/projects/{pid}/api-keys/batch-delete | Batch delete `{ids}` |

### Guard Rules (content-safety interception)

| Method | Path | Description |
|---|---|---|
| GET/POST | /api/admin/guard-rules | List / create (regex/keyword, input/output, scope) |
| DELETE | /api/admin/guard-rules/{rid} | Delete |
| POST | /api/admin/guard-rules/batch-delete | Batch delete `{ids}` |

### Alert Rules & Notification

| Method | Path | Description |
|---|---|---|
| GET/POST | /api/admin/alerts/rules | List / create alert rule (Prometheus expr, severity, duration) |
| PUT | /api/admin/alerts/rules/{rid} | Update rule |
| DELETE | /api/admin/alerts/rules/{rid} | Delete |
| POST | /api/admin/alerts/rules/batch-delete | Batch delete `{ids}` |
| POST | /api/admin/alerts/rules/{rid}/test | Dry-run rule (currently firing or not) |
| GET/PUT | /api/admin/notify-config | Notification channels (WeCom / email, DB-backed) |
| POST | /api/admin/notify-config/test | Send test notification |
| GET | /api/admin/alerts/events | Alert event stream `{limit}` |

### Login Security & System Config

| Method | Path | Description |
|---|---|---|
| GET/PUT | /api/admin/login-config | Login security params (IP whitelist, lockout threshold/duration) |
| GET | /api/admin/audit | Audit log (pagination + action/user/status filters) |
| GET | /api/admin/audit/actions | Audit action enum |
| GET/PUT | /api/admin/time-config | Timezone config |
| POST | /api/admin/time-config/probe | Probe host timezone |

## Cluster Upgrade /api/admin/upgrade (super_admin)

| Method | Path | Description |
|---|---|---|
| GET | /api/admin/upgrade/status | Per-component upgrade state (current pin / local image / runtime version / running task) |
| POST | /api/admin/upgrade/check | `{target, force?}` → available versions from Docker Hub (10min cache) |
| POST | /api/admin/upgrade/execute | `{target, to_version}` create upgrade task (pull new tag / rebuild + blue-green container switch) |
| GET | /api/admin/upgrade/tasks | Upgrade task history `{limit}` |

Components: vllm / sglang / llamafactory / gpu_agent (pull kind, tag swap); backend / web (build kind, rebuilt from host /repo + blue-green container recreate; self-upgrade converges via the scheduler).

## Datasets /api/datasets (developer+, tenant-scoped)

| Method | Path | Description |
|---|---|---|
| GET/POST | /api/datasets | List (search/paging) / create |
| DELETE | /api/datasets/{dsid} | Delete (409 when referenced) |
| POST | /api/datasets/batch-delete | Batch delete `{ids}` |
| GET | /api/datasets/tasks | Background dataset task status |
| GET | /api/datasets/template | Template download |
| POST | /api/datasets/import-s3 | Import from object storage |
| GET | /api/datasets/{dsid} | Detail |
| GET | /api/datasets/{dsid}/versions | Version list (DVC) |
| POST | /api/datasets/{dsid}/versions/{vid}/commit | Commit new version |
| GET | /api/datasets/{dsid}/versions/{vid}/samples | Sample preview |
| GET | /api/datasets/{dsid}/versions/{vid}/raw | Raw file |
| GET | /api/datasets/{dsid}/dvc-log | DVC operation log |
| POST | /api/datasets/{dsid}/versions/upload | Upload new version (multipart) |
| POST | /api/datasets/parse | File parsing (PDF/Office → text, MinerU) |
| POST | /api/datasets/clean | Data cleaning task |
| POST | /api/datasets/synth | Data synthesis task |
| POST | /api/datasets/{dsid}/annotate | Annotation task (Argilla) |

## Model Repository /api/models (developer+)

| Method | Path | Description |
|---|---|---|
| GET | /api/models | Asset list (search/category/lineage filters) |
| POST | /api/models/base | Register base model (local path / download) |
| GET | /api/models/search | Search HF / ModelScope |
| POST | /api/models/upload | Local upload (multipart, resumable) |
| POST | /api/models | Create asset record |
| PUT | /api/models/{aid}/meta | Update metadata (category/notes) |
| GET | /api/models/{aid} | Detail |
| GET | /api/models/{aid}/lineage | Lineage chain (base → LoRA → merged/quantized artifact) |
| DELETE | /api/models/{aid} | Delete (409 when referenced; quantized artifacts protected) |
| POST | /api/models/batch-delete | Batch delete `{ids}` |

## Training /api/training (developer+)

| Method | Path | Description |
|---|---|---|
| POST | /api/training/jobs | Create job (94 params aligned with LLaMA-Factory; multi-node/multi-GPU: nodes × GPUs-per-node, torchrun/DDP/DeepSpeed/FSDP) |
| GET | /api/training/jobs | Job list |
| GET | /api/training/jobs/{jid} | Detail (incl. full training-param snapshot) |
| GET | /api/training/jobs/{jid}/metrics | Training curves (loss/eval, via Prometheus) |
| GET | /api/training/jobs/{jid}/logs | Live logs |
| POST | /api/training/jobs/{jid}/cancel | Cancel (container paused, scene preserved) |
| DELETE | /api/training/jobs/{jid} | Delete record (artifacts protected) |
| POST | /api/training/jobs/{jid}/resume | Resume (continue from paused scene) |
| GET | /api/training/params | Param metadata (94 × 10 groups) |
| GET | /api/training/templates | Training templates |

## Evaluation /api/evals (developer+)

| Method | Path | Description |
|---|---|---|
| POST | /api/evals | Create eval (rule-based / LLM judge, baseline comparison) |
| GET | /api/evals | List |
| GET | /api/evals/{tid} | Detail |
| GET | /api/evals/{tid}/report | Report (JSON, downloadable Markdown) |
| POST | /api/evals/compare | Multi-task comparison |
| DELETE | /api/evals/{tid} | Delete |
| POST | /api/evals/batch-delete | Batch delete `{ids}` |

## Model Compression (Quantization) /api/quantize (developer+)

| Method | Path | Description |
|---|---|---|
| GET | /api/quantize/methods | Method metadata (GPTQ/AWQ/LLM.int8/GPTQ-v2 etc., incl. hardware requirements) |
| GET/POST | /api/quantize | Task list / create (4 quantization modes; artifacts auto-registered as merged assets with lineage) |
| GET | /api/quantize/{tid} | Detail |
| GET | /api/quantize/{tid}/logs | Logs |
| DELETE | /api/quantize/{tid} | Delete (registered artifacts of succeeded tasks protected, 400) |
| POST | /api/quantize/batch-delete | Batch delete `{ids}` |

## Inference Deployment /api/inference (developer+)

| Method | Path | Description |
|---|---|---|
| POST | /api/inference/deployments | Create deployment (vLLM/SGLang, advanced params: max_num_seqs/dtype/prefix_caching/chunked_prefill/served_model_name/api_key/extra_args etc.) |
| GET | /api/inference/deployments | List |
| GET | /api/inference/deployments/{did} | Detail (incl. openai_base_url example) |
| POST | /api/inference/deployments/{did}/start | Start |
| POST | /api/inference/deployments/{did}/stop | Stop |
| DELETE | /api/inference/deployments/{did} | Delete (stops first if running) |
| POST | /api/inference/deployments/batch-delete | Batch delete `{ids}` (running ones stopped first) |
| POST | /api/inference/deployments/{did}/scale | Scale replicas `{replicas}` |
| GET | /api/inference/deployments/{did}/logs | Engine logs |
| GET | /api/inference/deployments/{did}/metrics | Engine metrics (QPS/latency/KV cache, via Prometheus) |
| POST | /api/inference/deployments/{did}/adapters | LoRA hot-load `{name}` (attach adapter without restart) |
| DELETE | /api/inference/deployments/{did}/adapters/{name} | LoRA hot-unload |

### OpenAI-Compatible Proxy

`POST/GET /v1/{deploy_name}/{rest:path}` (via web 443 reverse proxy)

- Auth: `Authorization: Bearer <project API Key>` (when the Key is model-scoped, the request model is validated)
- Pass-through: `/v1/{deploy_name}/chat/completions` (incl. streaming SSE), `/v1/{deploy_name}/models`, etc.
- Guardrails: input/output matching a guard rule returns 451
- Returns 503 when the deployment is not running; served_model_name mapping auto-normalizes the request model

## Cluster Ops /api/cluster (developer+ read; node writes need super_admin)

| Method | Path | Description |
|---|---|---|
| GET | /api/cluster/nodes | Compute nodes (heartbeat/driver/CUDA/host CPU/mem/disk/net/GPU detail/DCGM health) |
| POST | /api/cluster/nodes | Register node (multi-node scaling) |
| PUT | /api/cluster/nodes/{nid} | Update node |
| DELETE | /api/cluster/nodes/{nid} | Remove node |
| GET | /api/cluster/nodes/{nid}/metrics | Node metric history `{minutes}` (Prometheus) |
| GET | /api/cluster/system | System metrics (CPU/mem/disk/net/OS) |
| GET | /api/cluster/queue | Task queue (queued/running, GPU usage) |
| PUT | /api/cluster/queue/{jid} | Adjust priority `{priority 1-9}` |
| POST | /api/cluster/queue/{jid}/promote | Promote to head of queue |
| GET | /api/cluster/utilization | Utilization summary |
| GET | /api/cluster/inference | Inference resource usage |

## Monitoring & Logs

| Method | Path | Description |
|---|---|---|
| GET | /api/dashboard/summary | Dashboard summary (jobs/resources/alerts aggregate) |
| GET | /api/logs/services | Service (container) list |
| GET | /api/logs | Container logs `{service, tail, ...}` |

## Object Storage /api/storage (super_admin)

| Method | Path | Description |
|---|---|---|
| GET | /api/storage | Storage overview (per-backend usage) |
| GET | /api/storage/objects | Object browse `{prefix, backend}` (S3 directory-style) |
| POST | /api/storage/objects/batch-delete | Batch delete `{names, backend}` (S3 idempotent semantics) |
| DELETE | /api/storage/objects | Delete single object `{name, backend}` |
| GET/POST | /api/storage/backends | Backend list / add (S3-compatible: endpoint/ak/sk/bucket) |
| POST | /api/storage/backends/{bid}/test | Connectivity test |
| DELETE | /api/storage/backends/{bid} | Delete backend |

## Image Management /api/images (super_admin)

| Method | Path | Description |
|---|---|---|
| GET | /api/images | Platform engine image inventory (version probe 5min cache + stale fallback, non-blocking; custom images; pull/build status) |
| GET | /api/images/custom | Custom image list |
| POST | /api/images/custom | Register custom image `{name: tag, note}` (domestic accelerators etc.) |
| DELETE | /api/images/custom/{cid} | Remove registration |
| POST | /api/images/custom/batch-delete | Batch remove `{ids}` |
| POST | /api/images/{key}/probe | Force version probe (synchronous; key: vllm/sglang/llamafactory/gpu-agent) |
| POST | /api/images/{key}/refresh | Background pull/rebuild `{force?}` (async, status polled via GET /api/images) |

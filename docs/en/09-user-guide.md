# User Guide

VerveDeck platform user guide (for platform users: ML engineers and ops administrators). Organized by the left-side menu, covering the complete workflow from login to model serving.

> 中文版本：[../09-用户手册.md](../09-用户手册.md)

## 0. Login & Account

- Open `https://<server-ip>` in a browser (port 443; accept the self-signed cert warning)
- Default admin account is `admin`; **password change is forced on first login**
- Avatar menu (top right): change password / logout
- Security: repeated failures lock the account (threshold/duration configured by admin under System → Login Security); IP whitelist supported

## 1. Dashboard

Home summary: GPU utilization, running jobs (training/inference), queue length, recent alerts, and asset counts (models/datasets). Cards link to the corresponding modules.

## 2. Data Center (Datasets)

**Creating a dataset**
1. Click "New", fill in name/description
2. Choose a source: local upload (resumable) / import from object storage / file parsing (PDF, Office → text via MinerU)
3. Versioning: every upload/commit creates a new version (managed by DVC); view version history, sample previews, and the DVC operation log

**Data processing**
- **Parse**: PDF/Office → structured text
- **Clean**: batch cleaning jobs (dedup/filter/normalize)
- **Synth**: data synthesis jobs (sample expansion)
- **Annotate**: opens the Argilla annotation UI

**Deletion**: per-row delete (with confirmation); select rows and click "Batch Delete" (confirmed; entries referenced by training/eval jobs report the failure reason while the rest are deleted).

## 3. Model Repository

**Acquiring models (4 ways)**
1. **Search & download** from Hugging Face / ModelScope (background download, resumable)
2. **Local upload** (multipart, resumable for large files)
3. **Register a base model** pointing to an existing host path
4. **Platform outputs**: merged training artifacts and quantized artifacts are registered automatically

**Lineage**: asset detail → "Lineage" shows the full chain (base → LoRA training → merged/quantized artifact); every artifact traces back to its source model and training job.

**Organization**: assets are listed flat with category filters and search; edit category/notes in the detail view.

## 4. Training Center

**Creating a training job (wizard)**
1. Pick the base model (must be ready)
2. Pick the dataset (required fields validated)
3. Configure parameters: 94 parameters in 10 groups (aligned with LLaMA-Factory). Common groups:
   - Basics: method (SFT/DPO/PPO/KTO), template, LoRA rank/alpha
   - Data: epochs, batch size, sequence length, packing
   - Distribution: **nodes × GPUs-per-node = total GPUs** (multi-node runs over the container network with torchrun; IB/RDMA supported)
   - Framework: native DDP / DeepSpeed ZeRO / FSDP
4. On submit the job enters the queue (queued while GPUs are insufficient; adjust priority under Cluster Ops → Compute Nodes)

**Job operations**
- **Cancel**: pauses the container, preserving the scene (with confirmation)
- **Resume**: continues from the paused scene
- Detail view: full parameter snapshot, training curves (loss/eval), live logs
- On completion: the LoRA artifact is registered in the repository with lineage; merge into a full model when needed

## 5. Evaluation Center

- Create an eval: model (optionally with a baseline) + eval set + method (rule-based / LLM judge)
- Report: score breakdown + baseline comparison, downloadable as Markdown
- Compare: select multiple eval tasks for side-by-side comparison
- Multi-select batch delete supported

## 6. Model Compression (Quantization)

- "New quantization": source model (a merged model) + method (GPTQ / AWQ / LLM.int8 / GPTQ-v2; the UI shows each method's hardware requirements)
- Artifacts are registered in the repository automatically (named `quant-Nb-*`), with lineage, and are directly deployable for inference
- Logs/status visible in real time; artifacts of succeeded tasks are protected (deleting the task never deletes the registered artifact)

## 7. Inference (Deployments / Routes / Backends / Cache — four sub-menus)

### 7.1 Deployments

**Creating a deployment**
1. Pick the model (a ready asset from the repository)
2. Pick the engine: vLLM (default) / SGLang; the effective image is managed by the image pin system (Images page)
3. **Inference configuration** (optional):
   - Inference backend: empty = built-in engine; pick a custom backend (registered on the Backends page) and optionally a version (empty = default version); custom backends do not support PD disaggregation
   - Cache service: when attached, instances get the `VD_CACHE_ENDPOINT` env var (degrades gracefully when the service is down)
4. Deployment topology (optional): PD disaggregation (P/D pools + NIXL/Mooncake KV transfer), node count, NCCL env vars
5. Runtime parameters: quantization / max_len / memory fraction / replicas / extended KV (vLLM CPU offload, SGLang HiCache) / extra LoRA
6. Advanced parameters (optional; empty = engine default): max_num_seqs, dtype, prefix_caching, chunked_prefill, served_model_name (the externally exposed model name), api_key (engine-level auth), extra_args
7. Submit → the backend runs a GPU admission pre-check first (400 with required/available GPU counts when short) → container starts → status becomes running

**Day-to-day operations**
- Start / Stop (confirmed) / Delete (running ones stop first; multi-select batch delete supported)
- **Edit deployment**: name/description/auto-restart/backend apply immediately; engine/replicas/runtime params/extended KV changes stop and rebuild instances automatically; PD deployments cannot change cooperative TP/replicas here (use Scaling for the P/D pools)
- **Instance view** (in the detail drawer): per-instance liveness and IP (cooperative replicas / PD P & D pools / router), single-instance rebuild (recreated in place, deployment status unchanged); **auto-restart on error** toggle (exponential backoff 30 s → 2 h; a manual start resets the backoff; shows cumulative error count and next retry time)
- Replica scaling (needs free GPUs); PD deployments scale per role (P/D)
- **LoRA hot-load**: attach a LoRA adapter from the detail view, effective instantly without restart; hot-unload warns that requests to it will fail immediately
- Logs / metrics (QPS, latency, KV cache, via Prometheus)

**Calling (OpenAI-compatible)**
```bash
# Create an API Key for the project under "Tenants & Projects"
curl https://<ip>/v1/<deploy_name>/chat/completions \
  -H "Authorization: Bearer <API_KEY>" \
  -d '{"model":"<served_model_name or deploy name>","messages":[{"role":"user","content":"hello"}]}'
```
- API Keys can be scoped to specific models
- Guardrail match returns 451; deployment not running returns 503
- Streaming supported (`"stream": true`, SSE)
- For the same model, add a **route** (see 7.2) to get weighted distribution and failover; the entry point becomes `/v1/<route_name>/`

### 7.2 Routes

- The route data plane is served by the Higress gateway: **weighted distribution** (relative weights are normalized to sum 100) + **fallback takeover** (fallback targets take over proportionally when all primary targets are down, and revert automatically on recovery) + endpoint-level retries (5xx / connection errors switch endpoints)
- The `/v1/<route_name>/` prefix is globally exclusive with deployment names (no path collisions); both sides are validated at creation
- Targets: one route holds multiple target deployments, each with a weight + fallback flag; changes in target deployment state converge automatically
- Enable / disable: disabling removes the traffic from the gateway (targets are kept; re-enabling restores it); every change applies on the next sync cycle — the page shows sync state (pending / synced / error with reason)
- The route name is the public API prefix and cannot be changed (delete and recreate to rename); deleting a synced route leaves the gateway residue to the scheduler's orphan cleanup

### 7.3 Backends

- **builtin** (vllm / sglang): system-seeded, not deletable; the effective image follows the image pin system (runtime upgrades reflect automatically); the version table shows the baseline version and parameter defaults
- **custom** (name must end with `-custom`): each version defines image + run-command template
  - Placeholders: `{{model_path}}` `{{port}}` `{{model_name}}` `{{worker_ip}}` `{{gpu_count}}` `{{gpu_ids}}` `{{VAR}}` (injected env vars); unknown placeholders are kept verbatim with a warning, never silently dropped
  - Health check path: liveness & metrics probe (default `/v1/models`)
  - Default env / default params (JSON, used for form defaults)
- YAML import: create-only, never overwrite (existing names reject the whole batch with per-item errors)
- Protection: a backend referenced by a deployment cannot be deleted; a referenced version cannot be renamed/deleted; custom backends do not support PD disaggregation

### 7.4 Cache Acceleration

Two levels:
1. **Per-deployment extended KV**: vLLM `--cpu-offload-gb` (GB of KV cache offloaded to host memory) / SGLang `--hicache-size` (host-memory cache tier in GB); set in the create/edit form or in the "Deployment Extended KV" section here; changes stop and rebuild the deployment's instances, effective after the rebuild
2. **Cache service instances** (managed containers, e.g. KV cache stores):
   - Empty command = run the image's default entrypoint; non-empty = executed via `bash -lc` inside the container (logs & exit code available)
   - Lifecycle: stopped → creating (waiting for the image) → running (health convergence; endpoint refreshed on IP drift); non-zero exit or 3 consecutive probe failures → failed (manual restart required)
   - Attached deployments get `VD_CACHE_ENDPOINT=http://ip:port` injected into their instances (gracefully empty when the service is down)
   - Editing a running service restarts it automatically; a service referenced by a deployment cannot be deleted

## 8. Cluster Ops

**Compute Nodes**
- Node cards: GPU total/in-use/free, driver, CUDA, host CPU/memory/disk/network, per-GPU utilization/memory/temp/power/clocks/ECC/Xid/throttling/health
- State: Online / Offline / Maintenance + a state message (last heartbeat time + troubleshooting hint); offline/recovery raise alerts automatically
- Maintenance mode: the wrench button on a node card → "Set maintenance" (optional reason) — removes the node from service: no offline detection, no alerts, its GPUs don't count toward scheduling capacity; "End maintenance" restores it
- Delete node: the delete button on a card, with confirmation; deletion also revokes all of the node's registration tokens so the node-side heartbeat daemon can't re-register it ("resurrect" it)
- Below: the task queue (priority +1/-1/promote-to-head) and the running-jobs list
- Multi-node scaling: super_admin can add/remove nodes

**Adding a Node** (3-step wizard, modeled on GPUStack's Add Worker)
The local node needs no manual registration — gpu-agent's heartbeat manages it. For remote nodes:
1. **Node info**: node name (e.g. `node-2`) + expected GPU count (optional; the node's own detection wins)
2. **Environment check**: a check script (nvidia-smi / platform reachability) to run on the target machine; continue once all checks pass
3. **Run command**: the server issues a 24h registration token (`vdk-` prefix) bound to the node name and renders a one-click copy command; "Revoke & reissue" is supported. On the target machine (scp the platform's `deploy/add-node.sh` there first):
   ```bash
   VD_PLATFORM=https://<platform-IP>:8443 VD_TOKEN=vdk-xxxx bash add-node.sh
   ```
   The script auto-completes: environment check → token-authorized registration (auto-collects GPU count/model/driver/CUDA/IB; node name comes from the token binding; idempotent) → starts a **background heartbeat daemon** (reports a live GPU snapshot every 10s; the platform marks the node offline after 90s of silence and alerts). Re-run the same command after a reboot to recover.
   Legacy mode: `VD_USER=admin VD_PASS=<password> bash add-node.sh [node-name]` (account login, no token).
   If the new machine should actually run jobs, deploy a full platform stack there too (`sudo bash install-from-iso.sh`); its node is then managed via gpu-agent heartbeats. Hosts with IB/RoCE NICs get RDMA devices injected into containers automatically (NCCL multi-node training over IB).

**GPU Devices**
- Cluster-wide GPU detail table: memory progress bar, temperature, power & power limit, SM/memory clocks, ECC errors, Xid, throttle reasons, Remap, health

**Monitoring Dashboard**
- Time-range switch (1h/6h/24h/7d)
- Dashboard selector (Grafana dashboards, embedded via the platform iframe, read-only anonymous Viewer)

**Metrics Catalog**
- Live catalog of every active metric in Prometheus: total active series, metric-type count, and distribution by category (host / inference / system / other)
- The metric table supports name search, category filter and series-count sorting; each row shows the metric name, category, source (node-exporter / DCGM / vLLM, etc.) and current series count
- Auto-refreshes every 30s; use it to audit "which metrics the platform collects and how many instances are covered" (complements the Grafana dashboards: dashboards show trends, this page shows coverage)

**Alert Rules**
- Notification channels: WeCom (group bot webhook) / email (SMTP), test send supported
- Threshold/event rules: custom category (capacity 5000, server-side pagination + category/type/state/name filters), metric/event type, threshold, severity, cooldown, active channels
- Ships with 39 built-in core rules (aligned with industry practice: covers all 21 alertable metrics with warning/critical tiers plus node/task/login event alerts), auto-created on startup and idempotent across restarts
- Bulk import: CSV, one rule per line

**Alert Events**
- Alert event stream (auto-refresh every 10s) with filters (level/target/ack state)
- Single & bulk acknowledge (ack, with acker/time), un-acknowledge, delete (confirmed)

**Log Center**
- Pick a container service to tail live logs (line count adjustable)

**Cluster Upgrade** (moved from System Management)
- Full platform image inventory (18 items, identical to the Image Management page): per-component current/available versions (Docker Hub) / one-click upgrade
- Upgrade modes: engines (vLLM/SGLang/LLaMA-Factory/evalscope) only update the runtime pin (new jobs pick it up); self-built (MinerU/backend/web) are rebuilt from the host source tree + blue-green container recreate; the other 11 monitoring/infrastructure components (DCGM Exporter, Node Exporter, Prometheus, Alertmanager, Grafana dark+light, PostgreSQL, Redis, OpenSearch, Argilla, RustFS) pull the new tag + blue-green recreate (impact shown in the UI)

## 9. Tenants & Projects

- Two-level structure: Tenant → Project; a project is the unit of resource isolation
- Project members: add/remove users (removal confirmed), change roles (confirmed)
- Roles: super_admin (global) / tenant_admin (tenant management) / developer (resource use) / viewer (read-only)
- Project API Keys: created here (for the OpenAI proxy), optionally model-scoped, batch delete supported

## 10. Storage Management

- Backends: add an S3-compatible backend (endpoint/ak/sk/bucket) → connectivity test
- Object browser: directory-style browsing, single-object delete (confirmed), **multi-select batch delete** (confirmed; selection clears when switching directory)
- Platform datasets and model files live in object storage — confirm no platform references before deleting objects

## 11. Image Management (super_admin)

- Full platform image inventory (18 items, same data source and order as the cluster upgrade page):
  - Engines (5): vLLM / SGLang / LLaMA-Factory / GPU Agent / evalscope
  - Self-built (3): MinerU parsing / platform backend / web frontend
  - Monitoring & infrastructure (10): DCGM Exporter / Node Exporter / Prometheus / Alertmanager / Grafana (dark + light, two containers) / PostgreSQL / Redis / OpenSearch / Argilla / RustFS
- Each row shows the image reference, source (official/self-built), local state, size, and version (auto-probed; first open shows "probing…" then fills in; images without a shell show the tag, self-built images show build info)
- Actions: Pull (official images) / Rebuild (self-built images, using the host source tree /repo as build context) / Force probe
- Custom images: register third-party images such as domestic accelerators (`name:tag` format, appended after the platform inventory) to extend deployment options

## 12. System Management

| Tab | Function |
|---|---|
| Users & Roles | User CRUD, role management |
| Guard Rules | Content-safety rules (regex/keyword, input/output interception, scope); batch delete |
| Login Security | IP whitelist, failure lockout threshold/duration |
| Theme & Appearance | Light/dark theme, 6 languages |
| About | Version, open-source license, GitHub link |

## 13. General Interaction Conventions

- **All high-risk operations are confirmed**: delete/stop/cancel/unload/remove member/change role/upgrade all require explicit confirmation
- **Deletion is multi-select everywhere**: select rows in any resource list → "Batch Delete (N)" → confirm; partial failures report reasons while successful items take effect
- **i18n**: UI available in zh-CN/zh-TW/ja/en/fr/es, switchable top right
- **Theme**: light/dark toggle (top right)

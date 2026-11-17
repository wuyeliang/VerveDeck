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

## 7. Inference

**Creating a deployment**
1. Pick the model (a ready asset from the repository)
2. Pick the engine: vLLM (default) / SGLang
3. Advanced parameters (optional; empty = engine default): max_num_seqs, dtype, prefix_caching, chunked_prefill, served_model_name (the externally exposed model name), api_key (engine-level auth), extra_args
4. Submit → container starts → status becomes running

**Day-to-day operations**
- Start / Stop (confirmed) / Delete (running ones stop first; multi-select batch delete supported)
- Scaling: scale to the target replica count
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

## 8. Cluster Ops

**Compute Nodes**
- Node cards: GPU total/in-use/free, driver, CUDA, host CPU/memory/disk/network, per-GPU utilization/memory/temp/power/clocks/ECC/Xid/throttling/health
- Below: the task queue (priority +1/-1/promote-to-head) and the running-jobs list
- Multi-node scaling: super_admin can register/remove nodes

**Adding a Node** (two ways)
1. **From the UI**: the "Add Node" button above the node list → enter a node name (e.g. `node-2`) + GPU count → OK. The local node needs no manual registration — gpu-agent's heartbeat registers it automatically; this entry is for remote nodes / resource records.
2. **Via script** (recommended, auto-detection): on the *new machine*, run the repo's `deploy/add-node.sh`. It collects local GPU count/model/driver/CUDA and IB HCAs (stored in the node's meta for cross-node RDMA training reference), then registers:
   ```bash
   VD_PLATFORM=https://<platform-IP> VD_USER=admin VD_PASS=<password> \
   bash add-node.sh [node-name]    # defaults to the local hostname
   ```
   A duplicate node name exits with an error (delete the old node first or use a different name). If the new machine should actually run jobs, deploy a full platform stack there too (`sudo bash install-from-iso.sh`); its node is then managed via gpu-agent heartbeats. Hosts with IB/RoCE NICs get RDMA devices injected into containers automatically (NCCL multi-node training over IB).

**GPU Devices**
- Cluster-wide GPU detail table: memory progress bar, temperature, power & power limit, SM/memory clocks, ECC errors, Xid, throttle reasons, Remap, health

**Monitoring Dashboard**
- Time-range switch (1h/6h/24h/7d)
- Dashboard selector (Grafana dashboards, embedded via the platform iframe, read-only anonymous Viewer)

**Alerts**
- Alert event stream with filters; rule management lives under System Management

**Log Center**
- Pick a container service to tail live logs (line count adjustable)

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

- Platform engine image inventory: image reference, local state, and version (auto-probed; first open shows "probing…" then fills in) for vLLM / SGLang / LLaMA-Factory / GPU Agent
- Actions: Pull (official images) / Rebuild (self-built images) / Force probe
- Custom images: register third-party images such as domestic accelerators (`name:tag` format) to extend deployment options

## 12. System Management

| Tab | Function |
|---|---|
| Users & Roles | User CRUD, role management |
| Guard Rules | Content-safety rules (regex/keyword, input/output interception, scope); batch delete |
| Alert Rules | Prometheus-expression rules, severity, duration, notification channels (WeCom/email, test send) |
| Login Security | IP whitelist, failure lockout threshold/duration |
| Cluster Upgrade | Per-component current/available versions (Docker Hub) / one-click upgrade; engine kinds only update the pin (new jobs pick it up), self-built images (evalscope evaluation / MinerU parsing) are rebuilt (evalscope only affects new jobs; MinerU container blue-green recreated), agent/platform kinds blue-green recreate containers (impact shown in the UI) |
| Theme & Appearance | Light/dark theme, 6 languages |
| About | Version, open-source license, GitHub link |

## 13. General Interaction Conventions

- **All high-risk operations are confirmed**: delete/stop/cancel/unload/remove member/change role/upgrade all require explicit confirmation
- **Deletion is multi-select everywhere**: select rows in any resource list → "Batch Delete (N)" → confirm; partial failures report reasons while successful items take effect
- **i18n**: UI available in zh-CN/zh-TW/ja/en/fr/es, switchable top right
- **Theme**: light/dark toggle (top right)

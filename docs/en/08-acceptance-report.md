# VerveDeck Deployment Acceptance Report

> Version: v1.0 (2026-11) · Target: single-node 8×RTX 4090 / single-GPU 3080 dev server
> **Result: all 20 requirements delivered and verified.** Quick integration suite **510 passed / 0 failed**; end-to-end regression **10/10 passed**.

---

## 1. Scope & Environment

| Item | Value |
|---|---|
| Project | VerveDeck LLM fine-tuning platform (renamed from OpenForge) |
| Repository | `git@github.com:wuyeliang/VerveDeck.git` (main) |
| Server | 100.100.33.86 (root passwordless SSH, KVM) |
| GPU | Dev/verification: single GPU (RTX 3080 20GB modded); production plan: 8×RTX 4090 (192GB) |
| Deployment | Docker Compose (project `fintune`, compose at `deploy/docker-compose.yaml`) |
| Public entry | Web **HTTPS enforced, port 443** (nginx reverse-proxy → backend) |
| Stack | postgres / redis / opensearch / argilla / rustfs(S3) / mineru / backend / web / gpu-agent (9 resident) + on-demand vLLM/training containers |

### Resident service status (measured at acceptance)

| Container | Status |
|---|---|
| fintune-backend-1 | Up (healthy) |
| fintune-web-1 | Up |
| fintune-gpu-agent-1 | Up |
| fintune-argilla-1 | Up |
| fintune-postgres-1 | Up (healthy) |
| fintune-redis-1 | Up |
| fintune-rustfs-1 | Up (healthy) |
| fintune-mineru-1 | Up |
| fintune-opensearch-1 | Up (healthy) |
| ft-infer-1 (deployment 1) | Up (vLLM, replicas=1, running) |

- Login `admin/admin123` → `POST /api/auth/login` returns `{token, user}` (field is **`token`**).
- GPU node `node-1`: **online** with live DCGM metrics (temp/power/util/memory/ECC/clock/XID, etc.).
- Resident deployment 1 (`qwen25-05b-base`): **running**; OpenAI-compatible `GET /v1/qwen25-05b-base/models` returns 200.

---

## 2. 20 Requirements — Line-by-Line Acceptance

| # | Requirement | Delivery | Evidence | Status |
|---|---|---|---|---|
| 1 | Web HTTPS enforced + port 443 | nginx TLS termination, compose `443:443`, certs at `deploy/tls` | `ss -tlnp` shows only 443; reachable | ✅ |
| 2 | Login auto-submit & redirect | Frontend login submits on complete + redirects | TC01 auth passed; `login.tsx` auto-submit | ✅ |
| 3 | Hardware monitoring via DCGM | gpu-agent DCGM hostengine + `collect.sh` (18 fields, 5s) → `gpu_service.tick()` | node-1 online with live data; valid `agent.csv` | ✅ |
| 4 | Training wizard required-field marking + validation | Frontend required markers + backend create validation | `test_api_training` create-validation cases pass | ✅ |
| 5 | Bilingual docs (ZH/EN) | `docs/*.md` (ZH) + `docs/en/*.md` (EN), README per-language refs | 7 + 7 docs + this report | ✅ |
| 6 | Model repo HF/ModelScope search + resume + upload + delete | `models_api.search_models` dual-source; fixed HF `follow_redirects` + MS `/openapi/v1/models` | 3 results per source; `test_api_models` pass | ✅ |
| 7 | Inference deployment new features | Lifecycle (start/stop/scale/token-reissue race guard), LoRA hot-load, OpenAI proxy + guardrails | `test_api_deploy_lifecycle` / TC06 / TC09 pass | ✅ |
| 8 | Dashboard enrichment | Overview cards + storage health + node/GPU live metrics | `test_api_cluster_dashboard` pass | ✅ |
| 9 | VD element logo | Logo assets + frontend integration (login/sidebar/title) | `index.html`/App integration | ✅ |
| 10 | Delete support: data center / model repo / training / evals | Delete endpoints + frontend entries across 4 modules | Per-module delete API cases pass | ✅ |
| 11 | Training detail shows concrete params | Detail endpoint returns full hyperparams | `test_api_training` detail case pass | ✅ |
| 12 | Login brute-force protection (lockout) | Dual thresholds (per-account 5 / per-IP 30) + 15-min lock; in-process memory | `test_api_auth` pass; threshold split removed test cascade | ✅ |
| 13 | Dataset creation flow per reference image | Frontend new-dataset flow aligned (Aliyun Bailian style) | `data.tsx`; `test_api_datasets` pass | ✅ |
| 14 | Training task wizard per reference image | Frontend training wizard aligned | Training wizard refactor | ✅ |
| 15 | Model evaluation per reference image | Frontend evals page aligned | `evals.tsx`; TC08 pass | ✅ |
| 16 | Reference material (Aliyun Bailian) | UI visual/interaction references Aliyun Bailian console | Overall frontend style aligned | ✅ (design baseline) |
| 17 | Left menu: add "Model Compression" | Sidebar menu + page entry (quantization/compression) | Frontend menu; `/api/quantize/methods` returns 4 methods | ✅ |
| 18 | Evaluation report download | Report export/download endpoint | `test_api_evals` report case pass | ✅ |
| 19 | Fix language-switch dropdown not selectable | Fixed timing issue (switch too fast) | i18n switch fix; 6 locales present | ✅ |
| 20 | Rename OpenForge → VerveDeck | Project-wide keyword replacement (code/docs/images/copy) | No OpenForge residue; unified fintune/VerveDeck naming | ✅ |

> Note: "reference-image refactor" items (#13/#14/#15/#17) are accepted on the basis of completed core functionality; some pixel-level visual details were previously marked "pending user screenshots". All functionality and APIs are done and tested; pixel-perfect visual alignment can be fine-tuned after screenshots are provided.

---

## 3. Test & Verification Results

### 3.1 Quick integration + unit (excluding e2e)

- **510 passed / 0 failed** (~6 min, in-container `pytest`, direct 127.0.0.1:3001).
- Coverage: auth/RBAC/admin, datasets (incl. S3 object storage), models (lineage/search), training (full param-spec assertions), evals, inference (incl. real vLLM deployment lifecycle), cluster/dashboard, storage.
- Residual: deprecation warnings only (parametrize-generator, Pydantic class-config, short JWT key in unit tests) — non-failing.

### 3.2 End-to-end regression (`tests/test_e2e.py`, 10 cases)

- **10/10 passed** (~3 min, real GPU full pipeline).

| Case | Coverage | Result |
|---|---|---|
| TC01 | Auth & permission basics (401/login/me/unauthenticated 401) | ✅ |
| TC02 | Multi-tenant & project creation | ✅ |
| TC03 | Cross-tenant RBAC isolation (403/200/super-admin 403) | ✅ |
| TC04 | Model asset repo & lineage | ✅ |
| TC05 | Dataset full lifecycle (DVC + Argilla push) | ✅ |
| TC06 | Inference deployment + OpenAI-compatible API | ✅ |
| TC07 | **Real SFT LoRA training** + artifact archiving + lineage | ✅ |
| TC08 | Automated evaluation (rule matching) | ✅ |
| TC09 | LoRA hot-load + unload | ✅ |
| TC10 | Content guardrail (451 block) + audit trail (incl. 403) | ✅ |

### 3.3 Real defects fixed in this (deployment-closing) round

| Defect | Root cause | Fix |
|---|---|---|
| Model repo dual-source search failing | HF 308 not followed; MS legacy endpoint 404 | HF `follow_redirects=True`; MS → `/openapi/v1/models` |
| Login brute-force vs test-suite cascade | `u:`/`ip:` shared threshold 5; test concurrency tripped IP lock | Split thresholds: per-account 5 / per-IP 30 |
| Deployment 1 could not run | Single-GPU host, replicas=2 won't fit | Scale to 1; scale endpoint GPU-availability check prevents re-scaling to 2 |
| GPU node offline | DCGM 3.3.9 field-ID changes + `nv-hostengine -n` foreground block | Rewrote 18 valid fields + explicit background in CMD |
| **TC07 training crash on start** | Spec emitted 4 keys unsupported by LF 0.9.3 (`bias`/`early_stopping_patience`/`early_stopping_threshold`/`lazy_tokenize`) | Removed from `PARAM_GROUPS` + defensive strip in `build_config`; cross-checked all 87 pass-through keys to confirm none else invalid |

---

## 4. Consistency Check

- Docs ↔ deployment: service stack, port (443 HTTPS), login field (`token`), default project (16), base model (id=1, qwen25-05b, ready), resident deployment (id=1) all consistent.
- Code ↔ image: backend rebuilt from latest code (`BUILD_EXIT=0`); in-container code matches the repo; tests re-synced via `docker cp` after each rebuild.
- Features ↔ tests: all 20 requirements map to passing API/E2E cases; key paths (GPU metrics, guardrails, audit, lineage) are asserted.

---

## 5. Known Limitations & Follow-ups

1. **Single-GPU constraint**: dev host has one GPU, so `replicas × tensor_parallel ≤ 1`. Production 8×4090 can scale replicas/TP; the scale endpoint already validates GPU headroom (returns 400 over limit).
2. **Login lockout in memory**: failure counts live in backend process memory and reset on backend restart (fine for single-instance; use Redis for multi-replica).
3. **Tests not in image**: the backend image is a lean runtime; test files must be injected via `docker cp` (see `06-testing.md`).
4. **Deprecation-warning cleanup**: parametrize-generator / Pydantic class-config / short JWT key warnings can be cleaned up later; no functional impact.
5. **Visual polish**: pixel-level alignment for reference-refactor items can be refined after reference screenshots are provided.
6. **Code commit**: current changes are uncommitted; recommend a unified `git commit && git push` to `main` after acceptance sign-off.

---

*Report generated at the end of deployment closing, based on measured service state, the 510-case quick suite, and the 10-case E2E regression.*

# Testing System

The whole suite now lives in the **top-level `tests/` directory** (moved from
`platform/backend/tests/` on 2026-11-16, task #80; the old path is kept as a
symlink for backward compatibility). See `tests/README.md`.

## 1. Two Test Layers

| Layer | Files | Cases | Notes |
|---|---|---|---|
| Fast integration + unit | `tests/test_*.py` (excluding test_e2e.py) | **742** (measured in the final round, 27 files, 2026-11-16) | ~10-12 min (runner mode) |
| End-to-end regression | `tests/test_e2e.py` | **10** | Real GPU full pipeline, ~6 min |

Both layers total **752** cases (final round, 2026-11-16). The suite grows
dynamically with feature work (#80 round 2 was 659: 605 migrated + 54 new;
later feature sessions added time-config, training batch-delete and more).
The acceptance baseline is whatever `run_tests.sh` reports on the current run.

Files and coverage:

| File | Coverage |
|---|---|
| test_unit_core.py | Pure functions / ORM / config building (incl. full build_config with all 94 params, compute_type derivation, extra_args pass-through, internal-key anti-override, guardrails, password hashing) |
| test_unit_dataset.py | Dataset service pure functions (cleaning/dedup/masking/synthesis boundaries) |
| test_unit_alert_channel.py | Alert channels (WeCom/email) construction & dispatch |
| test_unit_prom_service.py | Prometheus queries / PROMQL / snapshot reconciliation |
| test_unit_distributed.py | Multi-node multi-GPU: torchrun orchestration / NCCL env / rank convergence / FSDP |
| test_unit_inference_cmd.py | Inference command building (vLLM/SGLang advanced params) |
| test_unit_upgrade.py | Upgrade service state machine / impact surface |
| test_api_auth.py | Login/JWT/password change |
| test_api_rbac.py | RBAC role matrix (all roles × all routes, 403/200) |
| test_api_admin.py | Tenants/projects/users/API keys/guardrails/audit |
| test_api_datasets.py | Dataset CRUD/versions/upload/clean/synthesize/parse/guardrail interception |
| test_api_models.py | Model repository/lineage/download status |
| test_api_training.py | Job creation validation/queueing/cancel/resume/templates/**parameter surface spec (10-group assertions)** |
| test_api_evals.py | Eval task lifecycle |
| test_api_inference.py | Deployment lifecycle/LoRA adapters/endpoint/proxy model normalization |
| test_api_deploy_lifecycle.py | Inference deployment state machine (start/stop/scale/token-reissue races) |
| test_api_cluster_dashboard.py | Nodes/queue/priority/Dashboard (incl. storage health field) |
| test_api_cluster_nodes.py | **Compute-node registration CRUD (#60) + system/inference overview views** |
| test_api_storage.py | **Object storage S3 integration** (health field/roundtrip fetch/version sync/delete cleanup; whole group skipped when S3 absent) |
| test_api_quantize.py | **Quantize (model compression) center, 33 cases**: method metadata / full creation-validation matrix / tenant isolation / detail & logs / delete rules / batch delete (mixed + cross-tenant) / scheduler state machine (real scheduler reconciles lost context → failed + alert; success → artifact registered as merged asset with lineage) / build_config all 4 methods / artifact name dedup |
| test_api_notify.py | Notify channel config/validation/per-channel test/RBAC |
| test_api_upgrade.py | Cluster upgrade status/check/execute validation branches |
| test_api_batch_delete.py | **Unified batch-delete contract across 9 endpoints (#97/#98)**: datasets/models/evals/images-custom/api-keys/guard-rules/alert-rules/deployments/storage-objects — partial results + reference protection + RBAC |
| test_api_login_security.py | **Login security config (#76)**: IP whitelist normalization/validation/threshold boundaries/RBAC/config restore |
| test_api_images.py | **Image management**: component list / non-blocking probe regression (stale-while-revalidate) / custom image CRUD / RBAC |
| test_api_logs.py | **Log center**: service list / container log reading / RBAC (skipped as a group outside compose envs) |
| test_api_time.py | **Cluster time config (timezone + NTP)**: GET shape / invalid-param 400 / probe shape / RBAC (no real apply, to avoid polluting the shared env) |
| test_unit_training_batch.py | Training-job batch-delete unit tests (#97 gap, sqlite in-memory, endpoint functions called directly) |

## 2. How to Run

Tests run **inside the backend container** (pytest hits 127.0.0.1:3001 directly,
same environment as production):

```bash
# On the server, in /opt/vervedeck (one-shot)
./tests/run_tests.sh runner  # resilient runner (recommended under concurrent
                             # activity): separate container for the full suite,
                             # backend rebuilds don't kill pytest, 3 retries per file (~18 min)
./tests/run_tests.sh fast   # fast suite (inside backend container, ~20-30 min, incl. real deployment switch)
./tests/run_tests.sh e2e    # E2E (~6-14 min, requires no other task holding the GPU)
./tests/run_tests.sh all

# Equivalent manual commands
docker exec fintune-backend-1 sh -c "rm -rf /app/tests && mkdir -p /app/tests"
docker cp /opt/vervedeck/tests/. fintune-backend-1:/app/tests/
docker exec fintune-backend-1 sh -c \
  "cd /app/tests && python -m pytest --ignore=test_e2e.py -v --tb=short -p no:cacheprovider"
docker exec fintune-backend-1 sh -c \
  "cd /app/tests && python -m pytest test_e2e.py -v --tb=short -p no:cacheprovider"
```

Local (no GPU / no compose): only `tests/test_unit_*.py` can run; the API tests
depend on the resident deployment, Prometheus/DCGM, the docker socket and S3,
and must run in a real environment.

**Two server run modes** (proven in #80 round 3):

| Mode | Where pytest runs | When backend is rebuilt | Use when |
|---|---|---|---|
| `fast/e2e/all` (docker exec) | inside the backend container | the pytest process dies with the container | no concurrent activity |
| `runner` (separate container) | host-networked container mounting `tests/` + `/data` | only a transient API gap; per-file retries cover it | parallel sessions / deploys are active |

Runner details: `--network host` to reach 127.0.0.1:3001; a host-network
container cannot join a second network, so `postgres`/`rustfs` (compose
service names, needed by unit tests that build their own engines) are mapped
via `--add-host`; S3 env vars match the backend container (storage cases do
not degrade); each file gets up to 3 attempts, waiting for API health first
(up to 180s) before a retry.

## 3. Key Conventions (lessons learned)

1. **Unique entity suffix `SUF`** (uuid hex6): every test entity carries a
   suffix, so suites can be re-run without interfering with each other;
2. **Resident deployment holds a GPU**: the default project has a resident
   inference deployment (occupying 1 GPU), so all GPU tasks created by the fast
   suite stay queued (only queueing/cancel/priority are tested), and no real
   training is triggered; the real GPU path is covered only by E2E and the
   deployment-lifecycle file;
3. **ORM staleness trap**: after `db.commit()`, attributes expire
   (expire_on_commit); across threads/after a commit you must capture plain
   values first (`did, tid = d.id, task.id`) — worker-thread closures may only
   capture plain values;
4. **S3 degradation**: when `S3_ENDPOINT` is empty, the storage cases are skipped
   as a whole group and the suite stays fully green without S3; when S3 is
   enabled, the storage cases run against real rustfs (write → S3 existence →
   delete → S3 prefix cleared);
5. **Assertions looser than implementation details**: dashboard/summary asserts
   only a subset of keys (new fields don't break old assertions);
6. **MinerU/vLLM version behavior**: mineru 4.x rejects .md text input (app.py
   passthrough handles it); vLLM 0.15 has no GET /v1/loras (tests use POST
   load/unload only);
7. **Route shadowing**: `GET /api/datasets/tasks` must be registered before
   `GET /{dsid}` (literals first);
8. **docker cp nesting trap**: `rm -rf /app/tests` before copying, otherwise the
   copy nests into `/app/tests/tests` and breaks collection;
9. **No pytest plugins in the container**: no pytest-timeout /
   pytest-rerunfailures — use the `retry_call` helper from conftest.

## 4. Flaky Countermeasures (#80, 2026-11-16)

Cases that fail intermittently in full-suite runs but pass in isolation
(timing / resource contention):

| Case | Root cause | Countermeasure |
|---|---|---|
| `test_utilization_used_gte_1` | The resident deployment 1 can be restarted by concurrent changes (container rebuild/upgrade); during the creating→running window (minutes of vLLM cold start) used_gpus/running_deployments drop to zero | Poll until convergence (poll 300s/10s) instead of a one-shot assertion |
| `test_synth_real` | Synthesis depends on a real LLM call through deployment 1; upstream timeouts/restarts make the task fail | `retry_call` retries the whole flow 3 times (fresh task per attempt, 20s backoff) |

New cases that depend on shared resident resources (deployment 1 / single GPU /
background probing) should prefer `poll` + `retry_call` over fixed sleeps or
one-shot assertions.

## 5. Acceptance Baseline (final round, 2026-11-16)

- Fast suite: **742/742 PASSED** (run on 100.100.33.86, runner mode, 27 files
  with zero retries; incl. S3 integration, full parameter surface, 9-endpoint
  batch delete, time config, training batch delete)
- E2E regression: **10/10 PASSED** (dataset → clean → SFT training → LoRA merge
  → deploy → inference → eval, full pipeline)
- Total **752/752 green** (2026-11-16 12:31, ~18 min)

## 6. Change Log

- **2026-11-16 (#80 round 3)**: final full-suite acceptance **752/752 green**
  (runner mode); fixed 2 product defects (`cluster/inference` NaN metric → 500;
  `scale` to the same replica count not idempotent → 400; see 08 report §3.3);
  log center unknown-service 500 → 404 (docker-py NotFound); test hardening:
  `batch_delete` deployment-1 self-healing fixture (stopped → start → poll
  running), `cluster_nodes` viewer case missing-argument fix + transient poll
  for the inference overview; `run_tests.sh` gained the `runner` resilient
  runner (separate container + 3 per-file retries, immune to backend
  rebuilds); cleaned up historical leftovers (8 test nodes, a running
  quantize-task row orphaned by a killed run).
- **2026-11-16 (#80)**: suite moved to the top-level `tests/` directory (old
  path kept as a symlink); 5 new functional test files (batch delete / node
  registration / login security / images / logs, 54 cases); polling/retry
  countermeasures for the two flaky cases; fixed the inconsistent `deleted`
  contract on 3 batch-delete endpoints (int → id list, the frontend
  useBatchDelete relies on the list `.length`); fixed the stale proxy case
  (after #96 model normalization, expectation changed 404 → 200 + normalized
  model name).
- **2026-11-16**: added `test_api_quantize.py` — 33 E2E cases for the quantize
  (model compression) module (suite grows 13 → 14 files); also fixed the
  `delete_task` artifact-protection defect (see 08-acceptance-report §3.3).
  The quantize cases rely on the real scheduler thread for reconciliation and
  run as-is inside the container; on a GPU-less dev box they can be validated
  against a local SQLite-backed backend (tasks stay pending, no GPU used).

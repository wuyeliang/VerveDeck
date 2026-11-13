# Testing System

## 1. Two Test Layers

| Layer | Files | Cases | Notes |
|---|---|---|---|
| Fast integration + unit | `tests/test_*.py` (13 files, excluding test_e2e.py) | **510** (500 baseline + 10 new this round, see §4) | Run inside the container, ~15-25 min |
| End-to-end regression | `tests/test_e2e.py` | **10** | Real GPU full pipeline, ~14 min |

The 13 fast-suite files:

| File | Coverage |
|---|---|
| test_unit_core.py | Pure functions / ORM / config building (incl. full build_config with all 94 params, compute_type derivation, extra_args pass-through, internal-key anti-override, guardrails, password hashing) |
| test_unit_dataset.py | Dataset service pure functions (cleaning/dedup/masking/synthesis boundaries) |
| test_api_auth.py | Login/JWT/password change |
| test_api_rbac.py | RBAC role matrix (all roles × all routes, 403/200) |
| test_api_admin.py | Tenants/projects/users/API keys/guardrails/audit |
| test_api_datasets.py | Dataset CRUD/versions/upload/clean/synthesize/parse/guardrail interception |
| test_api_models.py | Model repository/lineage/download status |
| test_api_training.py | Job creation validation/queueing/cancel/resume/templates/**parameter surface spec (10-group assertions)** |
| test_api_evals.py | Eval task lifecycle |
| test_api_inference.py | Deployment lifecycle/LoRA adapters/endpoint |
| test_api_deploy_lifecycle.py | Inference deployment state machine (start/stop/scale/token-reissue races) |
| test_api_cluster_dashboard.py | Nodes/queue/priority/Dashboard (incl. storage health field) |
| test_api_storage.py | **Object storage S3 integration** (health field/roundtrip fetch/version sync/delete cleanup; whole group skipped when S3 absent) |

## 2. How to Run

Tests run **inside the backend container** (pytest hits 127.0.0.1:3001 directly,
same environment as production):

```bash
# Sync test files into the container (required after image rebuilds)
docker exec fintune-backend-1 mkdir -p /app/tests
docker cp /opt/vervedeck/platform/backend/tests/. fintune-backend-1:/app/tests/

# Fast suite (13 files, excluding e2e)
docker exec fintune-backend-1 sh -c \
  "cd /app/tests && python -m pytest test_unit_core.py test_unit_dataset.py test_api_auth.py \
   test_api_rbac.py test_api_admin.py test_api_datasets.py test_api_models.py \
   test_api_training.py test_api_evals.py test_api_inference.py test_api_deploy_lifecycle.py \
   test_api_cluster_dashboard.py test_api_storage.py -v --tb=short -p no:cacheprovider"

# E2E (real GPU, requires no task in use; ~14 min)
docker exec fintune-backend-1 sh -c \
  "cd /app/tests && python -m pytest test_e2e.py -v --tb=short -p no:cacheprovider"
```

## 3. Key Conventions (lessons learned)

1. **Unique entity suffix `SUF`** (uuid hex6): every test entity carries a
   suffix, so suites can be re-run without interfering with each other;
2. **Resident deployment holds a GPU**: the default project has a resident
   inference deployment (occupying 1 GPU), so all GPU tasks created by the 500
   suite stay queued (only queueing/cancel/priority are tested), and no real
   training is triggered; the real GPU path is covered only by E2E;
3. **ORM staleness trap**: after `db.commit()`, attributes expire
   (expire_on_commit); across threads/after a commit you must capture plain
   values first (`did, tid = d.id, task.id`) — worker-thread closures may only
   capture plain values;
4. **S3 degradation**: when `S3_ENDPOINT` is empty, test_api_storage is skipped
   as a whole group, and the 500 suite stays fully green without S3; when S3 is
   enabled, the storage cases run against real rustfs (write → S3 existence →
   delete → S3 prefix cleared);
5. **Assertions looser than implementation details**: dashboard/summary asserts
   only a subset of keys (new fields don't break old assertions);
6. **MinerU/vLLM version behavior**: mineru 4.x rejects .md text input (app.py
   passthrough handles it); vLLM 0.15 has no GET /v1/loras (tests use POST
   load/unload only); an unknown model returns 404, not 400;
7. **Route shadowing**: `GET /api/datasets/tasks` must be registered before
   `GET /{dsid}` (literals first).

## 4. Acceptance Baseline (Nov 2026, this round)

- Fast suite: **505/505 PASSED** (incl. S3 integration and the full parameter surface)
- E2E regression: **10/10 PASSED** (dataset → clean → SFT training → LoRA merge
  → deploy → inference → eval, full pipeline)
- md5 consistent across three locations: local = /opt/vervedeck source tree =
  /app inside the container

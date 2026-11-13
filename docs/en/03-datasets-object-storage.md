# Datasets & Object Storage

## 1. Data Model

- **Dataset**: a named dataset within a project
  (`/data/datasets/p{project_id}/{name}/`), an independent Git+DVC repo;
- **DatasetVersion**: immutable versions (`v1, v2, …`); each clean/synthesize/
  parse/upload produces a new version:
  - `data.jsonl`: normalized samples (alpaca / dpo format)
  - `raw.md`: (for document parsing) the original markdown
  - `fmt`: `alpaca` (instruction/input/output) or `dpo` (prompt/chosen/rejected)
- **DVC**: local metadata version control (remote at `/data/dvc-remote`);
  commit history via `dvc-log`;
- **Object storage (rustfs)**: the **primary store** for version files — see below.

## 2. Object Storage Architecture (rustfs / S3)

```
                version commit
  upload/parse/clean/synth ─────────────► local /data/datasets/.../v{n}/data.jsonl   (processing cache)
                                              │
                                              │ storage.put_dir (sync upload)
                                              ▼
                              rustfs bucket vervedeck-datasets
                              key = local path relative to /data
                              e.g. datasets/p16/customer-qa/v1/data.jsonl
                                              ▲
  train/eval/clean/synth read data ──► ensure_version_local ──► local hit? ─yes─► use directly
                                              │no
                                              ▼
                                   storage.ensure_local download from S3
```

**Conventions**:

| Operation | Behavior |
|---|---|
| Write (version commit) | Local cache first, then `put_dir` full upload to S3; upload failure only warns (local copy stays complete) |
| Read (all consumers) | Unified via `ensure_version_local` / `ensure_version_raw`: local hit → use directly; miss → fetch from S3 |
| Delete (dataset removal) | Local dir + DVC metadata + S3 prefix (`storage.delete_dir`) |
| Degradation | `S3_ENDPOINT` empty → all call sites auto-degrade to pure local, no functional difference |

**Read-path integration points** (all switched to `ensure_version_local`):
- Training: `training_service.build_config` (ensures local presence before symlinking data)
- Evaluation: `eval_service.build_test_file` (converts to test.jsonl)
- Data preview / raw file API
- Cleaning worker, synthesis worker (backend in-process threads)

**S3 key layout** (mirrors local 1:1):
```
vervedeck-datasets/
└── datasets/p{project_id}/{name}/v{n}/
    ├── data.jsonl
    └── raw.md            # produced only by document parsing
```

## 3. Data Production Pipeline

| Entry | Description | Execution |
|---|---|---|
| Upload JSONL/JSON/CSV | `POST /api/datasets/{id}/versions/upload` | Immediate, produces v{n} |
| Document parsing (MinerU) | PDF/Word/PPT/images → markdown → SFT samples (optional) | Resident mineru service (1 GPU) |
| Cleaning | Dedup / length filter / JSON repair / masking / guardrail detection | Backend thread |
| Synthesis | Generate QA / CoT data via an in-project inference deployment | Backend thread calling the OpenAI-compatible endpoint |
| Annotation | Push to Argilla → human annotation → collect as a new version | Argilla 2.x + OpenSearch |

All outputs produce a **new version** (immutable); training jobs always reference
a version id, keeping runs reproducible.

## 4. Guardrails (GuardRule)

- Regex rules, scope: `input`/`output`/`both`; project-level and global
  (`project_id=None`) supported;
- Input samples are checked at dataset creation/upload; the inference API proxy
  checks outputs;
- A hit rejects with 400 and writes an audit log; invalid regexes are skipped
  without crashing.

## 5. Model Repository & Lineage

- Base models: `POST /api/models/base` (hf_id) → scheduler thread
  `snapshot_download` (HF mirror) → ready;
- Successful training auto-registers a **LoRA artifact** (`type=lora`,
  recording parent/base, job_id, dataset_version_ids);
- Export-merged artifacts are `type=merged`; `GET /api/models/{id}/lineage`
  queries the lineage chain.

## 6. Capacity & Cleanup Recommendations

- The S3 bucket and local cache both occupy disk; the local cache can be cleaned
  once old versions are confirmed unused by training/eval
  (deleting local files does not affect the S3 primary store — the next read
  fetches it back automatically);
- Periodically assess bucket capacity with `mc du vervedeck/vervedeck-datasets`;
- DVC history (`.git`) grows with versions; run `dvc gc` long-term to purge
  unreferenced objects.

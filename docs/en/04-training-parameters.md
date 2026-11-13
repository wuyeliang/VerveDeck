# Full Training Parameter Reference (aligned with LLaMA-Factory WebUI)

The Training Center hyperparameter wizard is **driven by a parameter spec**:
`GET /api/training/params` returns 10 groups with **94 parameters**; the frontend
renders the form dynamically from it, and the backend
`training_service.build_config` assembles the LLaMA-Factory config from the same
spec.

- The parameter surface was checked one by one against **LLaMA-Factory 0.9.3**
  (the version bundled in the training image)
  `ModelArguments / DataArguments / FinetuningArguments` + HF
  `TrainingArguments`; all are parseable by `llamafactory-cli train`;
- **Group visibility** (identical front/back): the `LoRA` group shows only when
  finetuning type=lora; `Freeze` only when type=freeze; `DPO` only when
  stage=dpo; `Export` only when stage=export; all other groups are always shown;
- **Unknown-key pass-through**: keys in hyperparams outside the spec are written
  to the config as-is (forward-compatible with future LF parameters);
- **extra_args**: a JSON string; parsed and merged into the config with the
  **highest priority** (equivalent to the WebUI extra_args box);
- Platform-internal keys (`model_name_or_path`/`stage`/`output_dir`/`dataset`,
  etc.) cannot be overridden by users — the backend applies them as the final
  fallback.

## Data & Sequencing (data, expanded by default)

| Parameter | Type | Default | Range/Options |
|---|---|---|---|
| `template` chat template | str | qwen | qwen / llama3 / chatml / glm4 / deepseek / default |
| `cutoff_len` truncation length | int | 2048 | 8 ~ 131072 |
| `packing` sample packing | bool | False | — |
| `neat_packing` pack on sample boundaries | bool | False | — |
| `streaming` streaming read | bool | False | — |
| `max_samples` max samples per dataset | int | 100000 | ≥1 |
| `val_size` validation split ratio | float | 0 | 0 ~ 1 |
| `train_on_prompt` compute loss on prompt | bool | False | — |
| `mask_history` multi-turn: loss on last turn only | bool | False | — |
| `neftune_noise_alpha` NEFTune noise | float | 0 | 0 ~ 10 (0=off) |
| `preprocessing_num_workers` preprocessing procs | int | 4 | ≥1 |
| `dataloader_num_workers` | int | 1 | ≥0 |
| `dataloader_pin_memory` | bool | True | — |
| `dataloader_persistent_workers` | bool | False | — |
| `dataloader_prefetch_factor` | int | 10 | ≥1 |

## Training Strategy (train, expanded by default)

| Parameter | Type | Default | Range/Options |
|---|---|---|---|
| `learning_rate` | float | 1e-4 | ≥0 |
| `num_train_epochs` epochs | float | 3.0 | ≥0.1 |
| `max_steps` max steps | int | -1 | -1=by epochs |
| `lr_scheduler_type` | str | cosine | cosine / linear / constant / constant_with_warmup / polynomial / inverse_sqrt / reduce_lr_on_plateau / cosine_with_min_lr |
| `warmup_ratio` | float | 0.1 | 0 ~ 1 |
| `warmup_steps` (overrides ratio when >0) | int | 0 | ≥0 |
| `optim` optimizer | str | adamw_torch | adamw_torch / adamw_hf / adamw_8bit / adamw_bnb_8bit / sgd / adafactor |
| `adam_beta1` / `adam_beta2` / `adam_epsilon` | float | 0.9 / 0.999 / 1e-8 | — |
| `weight_decay` | float | 0 | 0 ~ 1 |
| `max_grad_norm` gradient clipping (0=off) | float | 1.0 | ≥0 |
| `per_device_train_batch_size` | int | 1 | ≥1 |
| `per_device_eval_batch_size` | int | 1 | ≥1 |
| `gradient_accumulation_steps` | int | 8 | ≥1 |
| `save_steps` | int | 200 | ≥1 |
| `save_total_limit` | int | 3 | ≥1 |
| `save_only_model` | bool | False | — |
| `save_strategy` | str | steps | no / steps / epoch / best |
| `logging_steps` | int | 5 | ≥1 |
| `eval_strategy` | str | no | no / steps / epoch |
| `early_stopping_patience` / `early_stopping_threshold` | int / float | 3 / 0 | — |
| `gradient_checkpointing` (saves VRAM) | bool | True | — |
| `lazy_tokenize` | bool | False | — |
| `seed` | int | 42 | — |
| `bias` | str | none | none / all |
| `enable_thinking` (Qwen3 etc.) | bool | True | — |
| `report_to` | str | tensorboard | none / wandb / mlflow / neptune / tensorboard / swanlab / all |
| `flash_attn` | str | auto | auto / fa2 / sdpa / eager |
| `rope_scaling` | str | — | linear / dynamic |
| `max_new_tokens` validation generation length | int | 512 | ≥1 |

## Precision & Quantization (precision)

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `compute_type` | str | bf16 | bf16 / fp16 / fp32 / pure_bf16 → backend derives the `fp16`/`bf16`/`pure_bf16` booleans (fp32=all off) |
| `quantization_bit` QLoRA | int | 0 | 0=no quantization; 4/8 (with the method below) |
| `quantization_method` | str | bitsandbytes | bitsandbytes / gptq / auto-gptq / hqq / eetq |

## LoRA (shown when finetuning type = lora)

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `lora_rank` | int | 8 | 1 ~ 1024 |
| `lora_alpha` | int | 32 | 1 ~ 2048 |
| `lora_dropout` | float | 0.05 | 0 ~ 1 |
| `lora_target` | str | all | all or module names |
| `additional_target` | str | — | extra modules (e.g. embed_tokens,lm_head) |
| `use_rslora` / `use_dora` | bool | False | RSLoRA / DoRA |
| `pissa` | bool | False | PiSSA init → derives `pissa_init`+`pissa_convert` |
| `loraplus_lr_ratio` | float | 0 | 0=LoRA+ off |
| `create_new_adapter` | bool | False | — |

## Freeze (shown when finetuning type = freeze)

| Parameter | Type | Default |
|---|---|---|
| `freeze_trainable_layers` trainable layers (-1=all) | int | 2 |
| `freeze_trainable_modules` | str | all |
| `freeze_extra_modules` | str | — |

## DPO Preference Alignment (shown when stage = dpo)

| Parameter | Type | Default | Notes |
|---|---|---|---|
| `pref_beta` KL coefficient | float | 0.1 | 0 ~ 1 |
| `pref_ftx` SFT loss weight | float | 0 | 0 ~ 10 |
| `pref_loss` | str | sigmoid | sigmoid / hinge / ipo / kto_pair / orpo / simpo |

## Multimodal VLM (always optional)

| Parameter | Type | Default |
|---|---|---|
| `freeze_vision_tower` / `freeze_multi_modal_projector` / `freeze_language_model` | bool | True / True / False |
| `image_max_pixels` / `image_min_pixels` | str | 768*768 / 32*32 |
| `video_max_pixels` / `video_min_pixels` | str | 256*256 / 16*16 |

## VRAM Optimization (GaLoRE / Apollo / BADAM)

| Parameter | Type | Default |
|---|---|---|
| `use_galore` (+ `galore_rank` 16 / `galore_update_interval` 200 / `galore_scale` 2.0 / `galore_target` all) | bool | False |
| `use_apollo` (+ `apollo_rank` 16 / `apollo_update_interval` 200 / `apollo_scale` 32.0 / `apollo_target` all) | bool | False |
| `use_badam` (+ `badam_mode` layer / `badam_switch_mode` ascending / `badam_switch_interval` 50 / `badam_update_ratio` 0.05) | bool | False |

## Export & Merge (shown when stage = export)

| Parameter | Type | Default |
|---|---|---|
| `export_size` shard (GB) | int | 2 |
| `export_device` | str | cpu (cpu / cuda) |
| `export_legacy_format` | bool | False |

## Others

| Parameter | Type | Notes |
|---|---|---|
| `cache_dir` | str | HF cache directory |
| `resume_from_checkpoint` | str | resume path (the UI "Resume" button auto-fills the latest checkpoint) |
| `extra_args` | str(JSON) | arbitrary LF parameters passed through as JSON, **highest priority** |

## Stage Templates (`GET /api/training/templates`)

Auto-applied when the wizard enters the hyperparameter step (overrides spec defaults):

| Stage | Key Defaults |
|---|---|
| cpt | cutoff_len 4096 · lr 2e-5 · epochs 1.0 · save 500 |
| sft | cutoff_len 2048 · batch 2 · lr 1e-4 · epochs 3 · lora 8/32 |
| dpo | batch 1 · lr 5e-6 · epochs 1.0 · lora 8/32 |
| export | export_size 2 · export_device cpu |

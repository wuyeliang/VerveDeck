# -*- coding: utf-8 -*-
"""核心逻辑单测：审计判定 / 安全原语 / 训练配置构建 / GPU 占用统计 / 护栏匹配

需要 ORM 的用例用 sqlite 内存库（平台模型无 postgres 专属类型）。
"""
import os
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models as M
from app.audit import _should_record
from app.security import (hash_password, verify_password, create_token,
                          decode_token, new_api_key)
from app.services import training_service as ts
from app.services import dataset_service as ds
from app.scheduler import _gpu_in_use
from app.api.inference import _check_guard


# ---------------- 审计判定 ----------------

@pytest.mark.parametrize("method,path,status,expect", [
    ("POST", "/api/admin/guard-rules", 200, True),     # 写操作
    ("PUT", "/api/datasets/clean", 201, True),
    ("DELETE", "/api/models/5", 200, True),
    ("PATCH", "/api/x", 500, True),
    ("GET", "/api/models", 200, False),                # 普通读
    ("GET", "/api/models", 401, True),                 # 安全事件：未授权
    ("GET", "/api/models", 403, True),                 # 安全事件：拒绝
    ("GET", "/api/admin/audit", 200, False),
    ("POST", "/api/dashboard/summary", 200, False),    # 跳过 dashboard
    ("GET", "/v1/dep/models", 200, False),             # 跳过 /v1 代理
    ("GET", "/health", 403, False),                    # 跳过 health
    ("GET", "/static/js/app.js", 401, False),          # 非 /api
    ("POST", "/api/auth/login", 401, True),            # 登录失败也留痕（写+401）
    ("GET", "/api/models", 404, False),                # 404 不算安全事件
    ("GET", "/api/models", 500, False),
])
def test_should_record(method, path, status, expect):
    assert _should_record(method, path, status) is expect


# ---------------- 安全原语 ----------------

def test_password_hash_verify():
    h = hash_password("s3cret-pw")
    assert verify_password("s3cret-pw", h) is True
    assert verify_password("wrong", h) is False
    assert hash_password("same") != hash_password("same")  # salt 随机


def test_password_verify_bad_hash():
    assert verify_password("x", "not-a-valid-hash") is False


def test_token_roundtrip(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "jwt_expire_hours", 1)

    class U:
        id, username, role = 42, "alice", "developer"
    tok = create_token(U())
    payload = decode_token(tok)
    assert payload["sub"] == "42"
    assert payload["username"] == "alice"
    assert payload["role"] == "developer"


def test_token_expired(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    import jwt as pyjwt
    bad = pyjwt.encode({"sub": "1", "exp": datetime.utcnow() - timedelta(hours=1)},
                       "unit-test-secret", algorithm="HS256")
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        decode_token(bad)
    assert ei.value.status_code == 401


def test_token_invalid_signature(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "jwt_secret", "unit-test-secret")
    import jwt as pyjwt
    forged = pyjwt.encode({"sub": "1"}, "other-secret", algorithm="HS256")
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        decode_token(forged)


def test_new_api_key_shape():
    raw, h = new_api_key()
    assert len(raw) >= 12
    assert len(h) == 64          # sha256 hex
    import hashlib
    assert h == hashlib.sha256(raw.encode()).hexdigest()
    raw2, _ = new_api_key()
    assert raw != raw2           # 每次唯一


# ---------------- 训练配置构建（sqlite） ----------------

@pytest.fixture()
def sdb(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    yield s
    s.close()


def _seed_job(s, tmp_path, stage="sft", ftype="lora", hp=None, dsvids=(1,), deepspeed="none"):
    # sqlite 内存库：NOT NULL 强约束（FK 默认不启用，created_by 可填占位 1）
    s.add(M.ModelAsset(id=1, project_id=1, name="base", type="base",
                       path="/data/models/hub/base", source="hf:x", status="ready",
                       created_by=1))
    s.commit()
    s.add(M.Dataset(id=1, project_id=1, name="ds1", created_by=1))
    s.commit()
    s.add(M.DatasetVersion(id=1, dataset_id=1, version=1, fmt="alpaca", created_by=1))
    s.commit()
    job = M.TrainingJob(id=99, project_id=1, name="job", stage=stage,
                        finetuning_type=ftype, base_model_id=1,
                        dataset_version_ids=list(dsvids), hyperparams=hp or {},
                        deepspeed=deepspeed, gpu_count=1, priority=5, status="queued",
                        submitted_by=1)
    s.add(job)
    s.commit()
    return job


def test_build_config_sft_lora(sdb, tmp_path):
    job = _seed_job(sdb, tmp_path, hp={"cutoff_len": 512, "lora_rank": 8})
    d = ts.build_config(job, sdb)
    assert d["stage"] == "sft"
    assert d["model_name_or_path"] == "/data/models/hub/base"
    assert d["cutoff_len"] == 512
    assert d["lora_rank"] == 8 and d["lora_alpha"] == 32
    assert d["dataset"] == "ds1"
    assert d["output_dir"].endswith("ckpt")
    assert d["report_to"] == "tensorboard"
    # dataset_info 落盘
    import json
    info = json.load(open(f"{ts.job_dir(99)}/dataset_info.json"))
    assert info["ds1"]["formatting"] == "alpaca"


def test_write_config_downgrade_report_to(sdb, tmp_path, monkeypatch):
    """官方镜像无 tensorboard：write_config 降级为 none（build_config 展示值不受影响）"""
    job = _seed_job(sdb, tmp_path)
    monkeypatch.setattr(ts, "image_has_tensorboard", lambda image: False)
    assert ts.build_config(job, sdb)["report_to"] == "tensorboard"
    import yaml
    d = yaml.safe_load(open(ts.write_config(job, sdb)))
    assert d["report_to"] == "none"


def test_write_config_keep_report_to(sdb, tmp_path, monkeypatch):
    """自构镜像装有 tensorboard：上报保持不变"""
    job = _seed_job(sdb, tmp_path)
    monkeypatch.setattr(ts, "image_has_tensorboard", lambda image: True)
    import yaml
    assert yaml.safe_load(open(ts.write_config(job, sdb)))["report_to"] == "tensorboard"


def test_read_metrics_trainer_state_fallback(tmp_path):
    """无 tfevents 时兜底解析 checkpoint-*/trainer_state.json（取步数最大的）"""
    from app.tfevents import read_metrics
    import json as _json
    jobdir = tmp_path / "job1"
    (jobdir / "runs").mkdir(parents=True)
    ck = jobdir / "ckpt"
    for step in (2, 10):
        (ck / f"checkpoint-{step}").mkdir(parents=True)
    (ck / "checkpoint-2" / "trainer_state.json").write_text(_json.dumps(
        {"log_history": [{"loss": 0.9, "learning_rate": 1e-4, "epoch": 0.1, "step": 2}]}))
    (ck / "checkpoint-10" / "trainer_state.json").write_text(_json.dumps(
        {"log_history": [
            {"loss": 0.9, "learning_rate": 1e-4, "epoch": 0.1, "step": 2},
            {"loss": 0.5, "learning_rate": 5e-5, "epoch": 0.5, "step": 5},
            {"loss": 0.3, "learning_rate": 1e-6, "epoch": 0.9, "step": 10},
        ]}))
    m = read_metrics(str(jobdir / "runs"))
    assert m["loss"] == [[2, 0.9], [5, 0.5], [10, 0.3]]
    assert m["learning_rate"][0][1] == 0.0001
    assert len(m["epoch"]) == 3
    assert read_metrics(str(tmp_path / "missing")) == {}


def test_build_config_dpo_pref(sdb, tmp_path):
    job = _seed_job(sdb, tmp_path, stage="dpo", dsvids=[1])
    v = sdb.get(M.DatasetVersion, 1)
    v.fmt = "dpo"
    sdb.commit()
    d = ts.build_config(job, sdb)
    assert d["stage"] == "dpo" and "pref_beta" in d
    import json
    info = json.load(open(f"{ts.job_dir(99)}/dataset_info.json"))
    assert info["ds1"]["formatting"] == "preference"


def test_build_config_cpt_mapping(sdb, tmp_path):
    job = _seed_job(sdb, tmp_path, stage="cpt")
    d = ts.build_config(job, sdb)
    assert d["stage"] == "pretrain"


def test_build_config_defaults(sdb, tmp_path):
    job = _seed_job(sdb, tmp_path, hp={})
    d = ts.build_config(job, sdb)
    assert d["cutoff_len"] == 2048
    assert d["learning_rate"] == 1e-4
    assert d["num_train_epochs"] == 3
    assert d["per_device_train_batch_size"] == 1
    assert d["gradient_accumulation_steps"] == 8


def test_build_config_no_dataset(sdb, tmp_path):
    job = _seed_job(sdb, tmp_path, dsvids=[])
    d = ts.build_config(job, sdb)
    assert "dataset" not in d and "dataset_dir" not in d


def test_checkpoints_sorted_desc(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    ck = ts.job_dir(7) + "/ckpt"
    for step in (10, 20, 5):
        os.makedirs(f"{ck}/checkpoint-{step}", exist_ok=True)
    os.makedirs(f"{ck}/checkpoint-abc", exist_ok=True)   # 非数字 step 忽略
    job = M.TrainingJob(id=7, project_id=1, name="j", stage="sft", status="succeeded")
    cks = ts.checkpoints(job)
    assert [c["step"] for c in cks] == [20, 10, 5]
    assert all(c["path"].startswith(ck) for c in cks)


# ---------------- GPU 占用统计 ----------------

def test_gpu_in_use_combines_jobs_and_deps(sdb):
    j1 = M.TrainingJob(id=1, project_id=1, name="j1", stage="sft", status="running",
                       gpu_count=2, submitted_by=1)
    j2 = M.TrainingJob(id=2, project_id=1, name="j2", stage="sft", status="queued",
                       gpu_count=8, submitted_by=1)
    d1 = M.Deployment(id=1, project_id=1, name="dep1", model_asset_id=1,
                      status="running", tensor_parallel=2, replicas=1, created_by=1)
    d2 = M.Deployment(id=2, project_id=1, name="dep2", model_asset_id=1,
                      status="stopped", tensor_parallel=4, replicas=2, created_by=1)
    sdb.add_all([j1, j2, d1, d2])
    sdb.commit()
    assert _gpu_in_use(sdb) == 2 + 2  # running job 2 卡 + 运行中部署 tp2


def test_gpu_in_use_empty(sdb):
    assert _gpu_in_use(sdb) == 0


def test_gpu_in_use_counting_creating(sdb):
    d = M.Deployment(id=1, project_id=1, name="d", model_asset_id=1,
                     status="creating", tensor_parallel=1, replicas=1, created_by=1)
    sdb.add(d)
    sdb.commit()
    assert _gpu_in_use(sdb) == 1


# ---------------- 护栏匹配 ----------------

def test_guard_match_and_scope(sdb):
    sdb.add(M.GuardRule(id=1, project_id=None, pattern=r"机密", scope="both",
                        description="全局规则", enabled=True))
    sdb.add(M.GuardRule(id=2, project_id=99, pattern=r"项目A词", scope="input",
                        description="项目级", enabled=True))
    sdb.add(M.GuardRule(id=3, project_id=None, pattern=r"输出词", scope="output",
                        description="输出规则", enabled=True))
    sdb.add(M.GuardRule(id=4, project_id=None, pattern=r"停用", scope="both",
                        description="已停用", enabled=False))
    sdb.commit()
    assert _check_guard(sdb, 16, "包含机密信息", "input") is not None
    assert _check_guard(sdb, 16, "正常内容", "input") is None
    assert _check_guard(sdb, 99, "提到项目A词", "input") is not None     # 项目级命中
    assert _check_guard(sdb, 16, "提到项目A词", "input") is None          # 跨项目不命中
    assert _check_guard(sdb, 16, "输出词", "input") is None               # scope=input 不匹配 output 规则
    assert _check_guard(sdb, 16, "输出词", "output") is not None
    assert _check_guard(sdb, 16, "停用", "input") is None                 # 停用规则不生效
    assert _check_guard(sdb, 16, "", "input") is None                     # 空文本


def test_guard_bad_regex_skipped(sdb):
    sdb.add(M.GuardRule(id=1, project_id=None, pattern=r"([bad", scope="both",
                        description="非法正则", enabled=True))
    sdb.commit()
    assert _check_guard(sdb, 1, "任意文本", "input") is None   # 非法正则跳过不崩


def test_guard_case_insensitive(sdb):
    sdb.add(M.GuardRule(id=1, project_id=None, pattern=r"secret", scope="both",
                        description="", enabled=True))
    sdb.commit()
    assert _check_guard(sdb, 1, "包含 SECRET 字", "input") is not None


# ---------------- 全量参数面（LLaMA-Factory 对齐） ----------------

def test_build_config_full_params_spec(sdb, tmp_path):
    job = _seed_job(sdb, tmp_path, hp={
        "compute_type": "fp16", "max_grad_norm": "0.5", "lora_dropout": "0.1",
        "packing": "true", "seed": "7", "extra_args": '{"foo": 1, "bar": "x"}',
    })
    d = ts.build_config(job, sdb)
    # 精度派生：fp16 → fp16=True bf16=False pure_bf16=False
    assert d["fp16"] is True and d["bf16"] is False and d["pure_bf16"] is False
    assert "compute_type" not in d
    # 用户覆盖（含字符串转类型）
    assert d["max_grad_norm"] == 0.5
    assert d["lora_dropout"] == 0.1
    assert d["packing"] is True
    assert d["seed"] == 7
    # extra_args JSON 透传且自身不入配置
    assert d["foo"] == 1 and d["bar"] == "x"
    assert "extra_args" not in d
    # spec 默认值生效
    assert d["optim"] == "adamw_torch" and d["save_total_limit"] == 3
    assert d["gradient_checkpointing"] is True and d["flash_attn"] == "auto"
    # 空值/零值清理
    assert "rope_scaling" not in d and "additional_target" not in d
    assert "neftune_noise_alpha" not in d and "quantization_bit" not in d
    # 内部键不可被透传覆盖
    assert d["model_name_or_path"] == "/data/models/hub/base"


def test_build_config_full_precision_and_fp32(sdb, tmp_path):
    job = _seed_job(sdb, tmp_path, hp={"compute_type": "fp32", "quantization_bit": "4"})
    d = ts.build_config(job, sdb)
    assert d["fp16"] is False and d["bf16"] is False and d["pure_bf16"] is False
    assert d["quantization_bit"] == 4 and d["quantization_method"] == "bitsandbytes"
    # 默认 bf16（新任务复用同一基座资产，不重跑 _seed_job 避免主键冲突）
    job2 = M.TrainingJob(id=101, project_id=1, name="job2", stage="sft",
                         finetuning_type="lora", base_model_id=1,
                         dataset_version_ids=[1], hyperparams={}, deepspeed="none",
                         gpu_count=1, priority=5, status="queued", submitted_by=1)
    sdb.add(job2)
    sdb.commit()
    d2 = ts.build_config(job2, sdb)
    assert d2["bf16"] is True and "fp16" in d2 and d2["fp16"] is False


def test_build_config_freeze_and_dpo_stage(sdb, tmp_path):
    job = _seed_job(sdb, tmp_path, stage="dpo", ftype="freeze")
    d = ts.build_config(job, sdb)
    assert d["finetuning_type"] == "freeze"
    assert d["freeze_trainable_layers"] == 2 and d["freeze_trainable_modules"] == "all"
    assert "freeze_extra_modules" not in d
    assert d["pref_beta"] == 0.1 and d["pref_ftx"] == 0 and d["pref_loss"] == "sigmoid"
    assert "lora_rank" not in d   # 非 LoRA 方式不带 LoRA 参数


def test_build_config_pissa_and_unknown_passthrough(sdb, tmp_path):
    job = _seed_job(sdb, tmp_path, hp={"pissa": True, "some_future_lf_arg": "keep"})
    d = ts.build_config(job, sdb)
    assert d.get("pissa_init") is True and d.get("pissa_convert") is True
    assert "pissa" not in d
    assert d["some_future_lf_arg"] == "keep"   # 未知键前向兼容透传


def test_build_config_export_stage_full(sdb, tmp_path):
    sdb.add(M.ModelAsset(id=2, project_id=1, name="lora1", type="lora",
                         path="/data/jobs/1/ckpt/checkpoint-10", source="hf:x",
                         status="ready", created_by=1))
    sdb.commit()
    job = M.TrainingJob(id=100, project_id=1, name="exp", stage="export",
                        finetuning_type="lora", base_model_id=1, adapter_asset_id=2,
                        dataset_version_ids=[], hyperparams={"export_size": "4",
                        "export_device": "cuda"}, deepspeed="none", gpu_count=1,
                        priority=5, status="queued", submitted_by=1)
    sdb.add(job)
    sdb.commit()
    d = ts.build_config(job, sdb)
    assert d["stage"] == "export" and "do_train" not in d
    assert d["adapter_name_or_path"] == "/data/jobs/1/ckpt/checkpoint-10"
    assert d["export_dir"].endswith("merged")
    assert d["export_size"] == 4 and d["export_device"] == "cuda"
    assert d["export_legacy_format"] is False

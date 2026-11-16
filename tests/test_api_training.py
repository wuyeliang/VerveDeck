# -*- coding: utf-8 -*-
"""训练中心 API：任务创建校验 / 查询 / 取消 / 排队优先级 / 断点续训 / 模板

GPU 被常驻部署占用 → 本文件创建的任务全部保持 queued（可安全测排队/取消），不占 GPU。
"""
import os

import pytest
import requests

from conftest import BASE, H, SUF, DEFAULT_PROJECT, BASE_MODEL_ASSET_ID


@pytest.fixture(scope="module")
def tctx(admin_tok, dsctx, succeeded_ckpt):
    """默认项目：导出用 LoRA 产物 + 各阶段排队任务"""
    c = {}
    # 本地 LoRA 产物（供 export 任务）
    aname = f"500texp-{SUF}"
    apath = f"/data/models/hub/{aname}"
    os.makedirs(apath, exist_ok=True)
    with open(os.path.join(apath, "a.txt"), "w") as f:
        f.write("x")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": aname,
                            "type": "lora", "path": apath})
    r.raise_for_status()
    c["adapter_id"] = r.json()["id"]
    # 未就绪基座（供"基座未就绪"校验）
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500tbase-{SUF}",
                            "hf_id": "org/none"})
    r.raise_for_status()
    c["pending_base_id"] = r.json()["id"]

    def mk_job(name, stage, versions, extra=None):
        body = {"project_id": DEFAULT_PROJECT, "name": name, "stage": stage,
                "finetuning_type": "lora", "base_model_id": BASE_MODEL_ASSET_ID,
                "dataset_version_ids": versions, "gpu_count": 1, "priority": 5}
        if extra:
            body.update(extra)
        r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok),
                          timeout=30, json=body)
        r.raise_for_status()
        return r.json()["id"]

    c["sft_id"] = mk_job(f"500tsft-{SUF}", "sft", [dsctx["alpaca_ver"]])
    c["dpo_id"] = mk_job(f"500tdpo-{SUF}", "dpo", [dsctx["dpo_ver"]])
    c["cpt_id"] = mk_job(f"500tcpt-{SUF}", "cpt", [dsctx["alpaca_ver"]])
    c["succeeded_ckpt"] = succeeded_ckpt
    return c


def _job(admin_tok, jid):
    r = requests.get(f"{BASE}/api/training/jobs/{jid}", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    return r.json()


# ---------- 列表 ----------

def test_list_200(admin_tok):
    r = requests.get(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_list_project_filter(admin_tok, tctx):
    rows = requests.get(f"{BASE}/api/training/jobs",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    assert all(j["project_id"] == DEFAULT_PROJECT for j in rows)


def test_list_status_filter(admin_tok, tctx):
    rows = requests.get(f"{BASE}/api/training/jobs",
                        params={"project_id": DEFAULT_PROJECT, "status": "queued"},
                        headers=H(admin_tok), timeout=15).json()
    assert rows and all(j["status"] == "queued" for j in rows)
    ids = [j["id"] for j in rows]
    assert tctx["sft_id"] in ids


def test_list_status_succeeded(admin_tok):
    """E2E 已产生成功任务"""
    rows = requests.get(f"{BASE}/api/training/jobs",
                        params={"project_id": DEFAULT_PROJECT, "status": "succeeded"},
                        headers=H(admin_tok), timeout=15).json()
    assert rows, "应存在 E2E 成功训练任务"


# ---------- 创建校验 ----------

def test_create_sft_queued(admin_tok, tctx):
    j = _job(admin_tok, tctx["sft_id"])
    assert j["status"] == "queued"
    assert j["stage"] == "sft"
    assert j["base_model"]


def test_create_dpo(admin_tok, tctx):
    j = _job(admin_tok, tctx["dpo_id"])
    assert j["stage"] == "dpo"


def test_create_cpt(admin_tok, tctx):
    j = _job(admin_tok, tctx["cpt_id"])
    assert j["stage"] == "cpt"


def test_create_bad_stage_400(admin_tok, dsctx):
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500tbs-{SUF}",
                            "stage": "bogus", "base_model_id": BASE_MODEL_ASSET_ID,
                            "dataset_version_ids": [dsctx["alpaca_ver"]]})
    assert r.status_code == 400


def test_create_no_versions_400(admin_tok):
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500tnv-{SUF}",
                            "stage": "sft", "base_model_id": BASE_MODEL_ASSET_ID})
    assert r.status_code == 400


def test_create_no_base_400(admin_tok, dsctx):
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500tnb-{SUF}",
                            "stage": "sft",
                            "dataset_version_ids": [dsctx["alpaca_ver"]]})
    assert r.status_code == 400


def test_create_missing_base_404(admin_tok, dsctx):
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500tmb-{SUF}",
                            "stage": "sft", "base_model_id": 999999,
                            "dataset_version_ids": [dsctx["alpaca_ver"]]})
    assert r.status_code == 404


def test_create_base_not_ready_400(admin_tok, tctx, dsctx):
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500tnr-{SUF}",
                            "stage": "sft", "base_model_id": tctx["pending_base_id"],
                            "dataset_version_ids": [dsctx["alpaca_ver"]]})
    assert r.status_code == 400


def test_create_export_no_adapter_400(admin_tok, dsctx):
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500ten-{SUF}",
                            "stage": "export", "base_model_id": BASE_MODEL_ASSET_ID,
                            "dataset_version_ids": [dsctx["alpaca_ver"]]})
    assert r.status_code == 400


def test_create_export_with_adapter_queued(admin_tok, tctx, dsctx):
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500tex-{SUF}",
                            "stage": "export", "base_model_id": BASE_MODEL_ASSET_ID,
                            "adapter_asset_id": tctx["adapter_id"],
                            "dataset_version_ids": [dsctx["alpaca_ver"]]})
    assert r.status_code == 200, r.text
    j = _job(admin_tok, r.json()["id"])
    assert j["status"] == "queued" and j["stage"] == "export"


# ---------- 查询 ----------

def test_get_job(admin_tok, tctx):
    j = _job(admin_tok, tctx["sft_id"])
    for k in ("id", "name", "stage", "status", "hyperparams", "gpu_count",
              "priority", "checkpoints", "error", "work_dir"):
        assert k in j, f"缺字段 {k}"


def test_get_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/training/jobs/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_get_checkpoints_empty(admin_tok, tctx):
    j = _job(admin_tok, tctx["sft_id"])
    assert j["checkpoints"] == []


def test_metrics_empty_ok(admin_tok, tctx):
    r = requests.get(f"{BASE}/api/training/jobs/{tctx['sft_id']}/metrics",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200  # 无 tensorboard 目录时安全返回


def test_logs_empty_ok(admin_tok, tctx):
    r = requests.get(f"{BASE}/api/training/jobs/{tctx['sft_id']}/logs",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert "logs" in r.json()


def test_metrics_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/training/jobs/999999/metrics",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_logs_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/training/jobs/999999/logs",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


# ---------- 取消 ----------

def test_cancel_queued(admin_tok, tctx):
    r = requests.post(f"{BASE}/api/training/jobs/{tctx['cpt_id']}/cancel",
                      headers=H(admin_tok), timeout=30)
    assert r.status_code == 200
    assert _job(admin_tok, tctx["cpt_id"])["status"] == "cancelled"


def test_cancel_cancelled_400(admin_tok, tctx):
    r = requests.post(f"{BASE}/api/training/jobs/{tctx['cpt_id']}/cancel",
                      headers=H(admin_tok), timeout=30)
    assert r.status_code == 400


def test_cancel_missing_404(admin_tok):
    r = requests.post(f"{BASE}/api/training/jobs/999999/cancel",
                      headers=H(admin_tok), timeout=30)
    assert r.status_code == 404


# ---------- 排队 / 优先级 ----------

def test_queue_contains(admin_tok, tctx):
    q = requests.get(f"{BASE}/api/cluster/queue", headers=H(admin_tok), timeout=15).json()
    ids = [j["id"] for j in q["queued"]]
    assert tctx["sft_id"] in ids


def test_queue_priority_order(admin_tok, tctx):
    """同排队任务中 priority 9 应排在 priority 1 之前"""
    r = requests.put(f"{BASE}/api/cluster/queue/{tctx['sft_id']}",
                     headers=H(admin_tok), timeout=15, json={"priority": 9})
    assert r.status_code == 200
    r = requests.put(f"{BASE}/api/cluster/queue/{tctx['dpo_id']}",
                     headers=H(admin_tok), timeout=15, json={"priority": 1})
    assert r.status_code == 200
    q = requests.get(f"{BASE}/api/cluster/queue", headers=H(admin_tok), timeout=15).json()
    prios = [j["priority"] for j in q["queued"]]
    assert prios == sorted(prios, reverse=True)
    i9 = next(i for i, j in enumerate(q["queued"]) if j["id"] == tctx["sft_id"])
    i1 = next(i for i, j in enumerate(q["queued"]) if j["id"] == tctx["dpo_id"])
    assert i9 < i1, "高优先级应排前"


def test_set_priority_clamp_high(admin_tok, tctx):
    r = requests.put(f"{BASE}/api/cluster/queue/{tctx['sft_id']}",
                     headers=H(admin_tok), timeout=15, json={"priority": 99})
    assert r.status_code == 200 and r.json()["priority"] == 9


def test_set_priority_clamp_low(admin_tok, tctx):
    r = requests.put(f"{BASE}/api/cluster/queue/{tctx['sft_id']}",
                     headers=H(admin_tok), timeout=15, json={"priority": 0})
    assert r.status_code == 200 and r.json()["priority"] == 1
    # 恢复
    requests.put(f"{BASE}/api/cluster/queue/{tctx['sft_id']}",
                 headers=H(admin_tok), timeout=15, json={"priority": 5})


def test_set_priority_not_queued_400(admin_tok, tctx):
    r = requests.put(f"{BASE}/api/cluster/queue/{tctx['cpt_id']}",
                     headers=H(admin_tok), timeout=15, json={"priority": 9})
    assert r.status_code == 400  # 已取消


def test_set_priority_missing_404(admin_tok):
    r = requests.put(f"{BASE}/api/cluster/queue/999999",
                     headers=H(admin_tok), timeout=15, json={"priority": 5})
    assert r.status_code == 404


def test_promote(admin_tok, tctx):
    r = requests.post(f"{BASE}/api/cluster/queue/{tctx['sft_id']}/promote",
                      headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    j = _job(admin_tok, tctx["sft_id"])
    assert j["priority"] == 9


def test_promote_missing_404(admin_tok):
    r = requests.post(f"{BASE}/api/cluster/queue/999999/promote",
                      headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


# ---------- 断点续训 ----------

def test_resume_no_ckpt_400(admin_tok, tctx):
    r = requests.post(f"{BASE}/api/training/jobs/{tctx['sft_id']}/resume",
                      headers=H(admin_tok), timeout=30, json={})
    assert r.status_code == 400, "无 checkpoint 任务不可续训"


def test_resume_with_ckpt(admin_tok, tctx):
    if not tctx["succeeded_ckpt"]:
        pytest.skip("无带 checkpoint 的成功任务")
    src = tctx["succeeded_ckpt"]["job_id"]
    r = requests.post(f"{BASE}/api/training/jobs/{src}/resume",
                      headers=H(admin_tok), timeout=30, json={})
    assert r.status_code == 200, r.text
    new = r.json()
    assert new["status"] == "queued"
    assert new["resume_from_job_id"] == src
    assert "resume_from_checkpoint" in new["hyperparams"]
    # 清理：取消新任务
    requests.post(f"{BASE}/api/training/jobs/{new['id']}/cancel",
                  headers=H(admin_tok), timeout=30)


# ---------- 模板 ----------

def test_templates(admin_tok):
    r = requests.get(f"{BASE}/api/training/templates", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert set(("cpt", "sft", "dpo", "export")) <= set(body.keys())
    assert body["sft"]["lora_rank"] == 8


def test_queue_running_structure(admin_tok):
    q = requests.get(f"{BASE}/api/cluster/queue", headers=H(admin_tok), timeout=15).json()
    for j in q["running"]:
        assert {"id", "name", "stage", "gpu_count", "started_at"} <= set(j.keys())


# ---------- 全量参数面 ----------

def test_params_spec_structure(admin_tok):
    r = requests.get(f"{BASE}/api/training/params", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    groups = r.json()
    assert isinstance(groups, list) and groups
    gkeys = {g["key"] for g in groups}
    assert {"data", "train", "precision", "lora", "freeze", "dpo", "mm", "mem", "export", "other"} <= gkeys
    for g in groups:
        assert g["label"] and g["params"]
        for p in g["params"]:
            assert p["key"] and p["label"] and p["type"] in ("int", "float", "bool", "str")
    # 覆盖 LLaMA-Factory WebUI 关键参数
    allkeys = {p["key"] for g in groups for p in g["params"]}
    for k in ("learning_rate", "num_train_epochs", "max_grad_norm", "lora_rank", "lora_alpha",
              "use_rslora", "use_dora", "pref_beta", "pref_loss", "packing", "neat_packing",
              "val_size", "neftune_noise_alpha", "flash_attn", "rope_scaling", "quantization_bit",
              "gradient_checkpointing", "freeze_vision_tower", "use_galore", "use_badam",
              "extra_args", "resume_from_checkpoint"):
        assert k in allkeys, f"缺少参数 {k}"

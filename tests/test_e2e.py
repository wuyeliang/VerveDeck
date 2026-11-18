# -*- coding: utf-8 -*-
"""
VerveDeck 端到端测试（13 个用例）

覆盖七大模块：
  TC01 认证与权限基础              安全治理
  TC02 多租户与项目创建            安全治理/运维
  TC03 跨租户 RBAC 隔离            安全治理
  TC04 模型资产仓库与血缘          模型资产
  TC05 数据集全生命周期(DVC/Argilla) 数据工程
  TC06 推理部署 + OpenAI 兼容 API   推理
  TC07 SFT LoRA 训练 + 产物 + 血缘  微调引擎/模型资产
  TC08 自动化评测(规则匹配)         评测
  TC09 LoRA 热加载                 推理
  TC10 内容护栏 + 审计日志          安全治理
  TC11 SFT LoRA + 2卡 ZeRO-2 + 高级超参（extra_args 透传）  微调引擎
  TC12 SFT 全参微调 + 高级超参      微调引擎
  TC13 export 合并 LoRA + export 高级参数  微调引擎/模型资产

前置条件：
  - 常驻栈（docker compose）已启动，backend 正常
  - 默认项目(16) 已注册基座模型资产 id=1 (qwen25-05b, status=ready)
  - 推理部署 id=1 (name=qwen25-05b-base) 已创建

运行方式（backend 容器内，pytest 直连本进程 3001 端口）：
  docker exec fintune-backend-1 pip install -q pytest
  docker exec fintune-backend-1 bash -lc \
    "cd /app/tests && python -m pytest test_e2e.py -v --tb=short -p no:cacheprovider"
"""
import json
import os
import time
import uuid

import pytest
import requests

BASE = "http://127.0.0.1:3001"
ADMIN = ("admin", "admin123")
SUFFIX = uuid.uuid4().hex[:6]
# 默认项目 / 基座资产 / 常驻部署 的 ID 由 conftest._discover_env() 动态发现（任何全新部署可跑）
from conftest import DEFAULT_PROJECT, BASE_MODEL_ASSET_ID, DEPLOY_ID, DEPLOY_NAME

STATE: dict = {}

SAMPLES = [
    {"instruction": f"请把标记词 e2e-{SUFFIX} 原样重复一遍。", "input": "", "output": f"e2e-{SUFFIX}"},
    {"instruction": "1+1 等于几？只回答数字。", "input": "", "output": "2"},
    {"instruction": "水的化学式是什么？", "input": "", "output": "H2O"},
    {"instruction": "苹果是什么颜色？", "input": "", "output": "红色"},
    {"instruction": "Python 的列表去重用什么内置类型？", "input": "", "output": "set"},
]


# ---------------- helpers ----------------

def _login(u: str, p: str) -> str:
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": u, "password": p}, timeout=15)
    r.raise_for_status()
    return r.json()["token"]


def H(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def poll(fn, timeout: float, interval: float = 5.0, desc: str = ""):
    """轮询 fn()，返回首个 truthy 结果；超时抛 AssertionError（附最后状态）。"""
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"{desc} 超时({timeout:.0f}s)，最后状态: {last}")


def _deploy() -> dict:
    r = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                     headers=H(STATE["admin"]), timeout=15)
    return r.json() if r.status_code == 200 else {}


def _wait_deploy_ready(timeout: float = 900.0):
    def check():
        d = _deploy()
        if d.get("status") != "running":
            return None
        rr = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models",
                          headers=H(STATE["admin"]), timeout=8)
        return rr if rr.status_code == 200 else None
    return poll(check, timeout=timeout, desc="推理部署就绪")


def _ensure_deploy_running():
    """以 OpenAI 端点实际可达为准（DB 状态可能先于端点就绪）。"""
    try:
        if requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models",
                        headers=H(STATE["admin"]), timeout=8).status_code == 200:
            return
    except requests.RequestException:
        pass
    d = _deploy()
    if d.get("status") != "running":
        r = requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/start",
                          headers=H(STATE["admin"]), timeout=15)
        assert r.status_code == 200, f"启动部署失败: {r.text}"
    _wait_deploy_ready()


@pytest.fixture(scope="module", autouse=True)
def _admin_token():
    """保证 admin token 可用（支持 -k 单独运行部分用例）。"""
    if "admin" not in STATE:
        STATE["admin"] = _login(*ADMIN)
    yield


# ---------------- TC01 认证与权限基础 ----------------

def test_tc01_auth():
    # 错误密码 -> 401
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": "admin", "password": "wrong-pw"}, timeout=15)
    assert r.status_code == 401, f"错误密码应返回 401，实际 {r.status_code}"
    # 管理员登录
    tok = _login(*ADMIN)
    STATE["admin"] = tok
    me = requests.get(f"{BASE}/api/auth/me", headers=H(tok), timeout=15)
    assert me.status_code == 200
    assert me.json()["username"] == "admin"
    # 未带 token 访问受保护接口 -> 401
    r = requests.get(f"{BASE}/api/models", timeout=15)
    assert r.status_code == 401, f"未认证应返回 401，实际 {r.status_code}"


# ---------------- TC02 多租户与项目创建 ----------------

def test_tc02_tenant_project():
    tok = STATE["admin"]
    r = requests.post(f"{BASE}/api/admin/tenants",
                      json={"name": f"e2e-tenant-{SUFFIX}"}, headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text
    STATE["tenant_id"] = r.json()["id"]

    r = requests.post(f"{BASE}/api/admin/projects",
                      json={"tenant_id": STATE["tenant_id"], "name": f"e2e-proj-{SUFFIX}"},
                      headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text
    STATE["proj_id"] = r.json()["id"]

    r = requests.get(f"{BASE}/api/admin/projects", headers=H(tok), timeout=15)
    assert r.status_code == 200
    assert any(p["id"] == STATE["proj_id"] for p in r.json()), "新建项目未出现在列表中"


# ---------------- TC03 跨租户 RBAC 隔离 ----------------

def test_tc03_rbac():
    tok = STATE["admin"]
    uname = f"e2e-dev-{SUFFIX}"
    r = requests.post(f"{BASE}/api/admin/users",
                      json={"username": uname, "password": "pass12345",
                            "role": "developer", "tenant_id": STATE["tenant_id"]},
                      headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text
    uid = r.json()["id"]
    STATE["dev"] = (uname, "pass12345")

    # 加入新租户项目
    r = requests.put(f"{BASE}/api/admin/projects/{STATE['proj_id']}/members",
                     json={"user_id": uid, "role": "developer"}, headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text

    dtok = _login(*STATE["dev"])
    STATE["dev_token"] = dtok
    # 开发者访问默认租户的项目 -> 403
    r = requests.get(f"{BASE}/api/models", params={"project_id": DEFAULT_PROJECT},
                     headers=H(dtok), timeout=15)
    assert r.status_code == 403, f"跨租户访问应 403，实际 {r.status_code}"
    # 开发者访问本租户项目 -> 200
    r = requests.get(f"{BASE}/api/models", params={"project_id": STATE["proj_id"]},
                     headers=H(dtok), timeout=15)
    assert r.status_code == 200, r.text
    # 开发者无权调用超管接口
    r = requests.get(f"{BASE}/api/admin/users", headers=H(dtok), timeout=15)
    assert r.status_code == 403, f"超管接口应 403，实际 {r.status_code}"


# ---------------- TC04 模型资产仓库与血缘 ----------------

def test_tc04_model_repo():
    tok = STATE["admin"]
    r = requests.get(f"{BASE}/api/models", params={"project_id": DEFAULT_PROJECT},
                     headers=H(tok), timeout=15)
    assert r.status_code == 200
    ready = [a for a in r.json() if a["status"] == "ready" and a["type"] == "base"]
    assert ready, "不存在 ready 状态的基座模型资产"
    m = next((a for a in ready if a["id"] == BASE_MODEL_ASSET_ID), ready[0])
    STATE["model"] = m
    assert m["size_bytes"] > 0, "基座模型资产大小异常"

    r = requests.get(f"{BASE}/api/models/{BASE_MODEL_ASSET_ID}", headers=H(tok), timeout=15)
    assert r.status_code == 200
    r = requests.get(f"{BASE}/api/models/{BASE_MODEL_ASSET_ID}/lineage",
                     headers=H(tok), timeout=15)
    assert r.status_code == 200
    assert r.json().get("asset", {}).get("id") == BASE_MODEL_ASSET_ID


# ---------------- TC05 数据集全生命周期（DVC + Argilla） ----------------

def test_tc05_dataset():
    tok = STATE["admin"]
    dname = f"e2e-ds-{SUFFIX}"
    r = requests.post(f"{BASE}/api/datasets",
                      json={"project_id": DEFAULT_PROJECT, "name": dname},
                      headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text
    dsid = r.json()["id"]
    STATE["ds_id"] = dsid

    # 上传 alpaca 样本 -> 生成 v1
    files = {"file": ("samples.json",
                      json.dumps(SAMPLES, ensure_ascii=False).encode("utf-8"),
                      "application/json")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      params={"fmt": "alpaca"}, files=files, headers=H(tok), timeout=30)
    assert r.status_code == 200, r.text
    ver = r.json()
    STATE["ver"] = ver
    assert ver["sample_count"] == len(SAMPLES), f"样本数不符: {ver}"

    r = requests.get(f"{BASE}/api/datasets/{dsid}/versions", headers=H(tok), timeout=15)
    assert r.status_code == 200 and len(r.json()) == 1

    # 样本预览
    r = requests.get(f"{BASE}/api/datasets/{dsid}/versions/{ver['id']}/samples",
                     headers=H(tok), timeout=15)
    assert r.status_code == 200

    # DVC 版本提交
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/{ver['id']}/commit",
                      headers=H(tok), timeout=60)
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True and r.json().get("dvc_rev"), r.text
    r = requests.get(f"{BASE}/api/datasets/{dsid}/dvc-log", headers=H(tok), timeout=15)
    assert r.status_code == 200
    assert r.json(), "DVC 日志为空"

    # 推送 Argilla 标注
    r = requests.post(f"{BASE}/api/datasets/{dsid}/annotate",
                      json={"version_id": ver["id"]}, headers=H(tok), timeout=120)
    assert r.status_code == 200, f"Argilla 推送失败: {r.text}"


# ---------------- TC06 推理部署 + OpenAI 兼容 API ----------------

def test_tc06_inference():
    tok = STATE["admin"]
    d = _deploy()
    assert d, "推理部署不存在"
    _ensure_deploy_running()

    # OpenAI 兼容：模型列表
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models", headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text
    names = [m["id"] for m in r.json().get("data", [])]
    assert DEPLOY_NAME in names, f"模型列表缺少 {DEPLOY_NAME}: {names}"

    # OpenAI 兼容：对话补全
    r = requests.post(f"{BASE}/v1/{DEPLOY_NAME}/chat/completions", headers=H(tok),
                      json={"model": DEPLOY_NAME,
                            "messages": [{"role": "user", "content": "只回复两个字母: OK"}],
                            "max_tokens": 16, "temperature": 0},
                      timeout=120)
    assert r.status_code == 200, r.text
    content = r.json()["choices"][0]["message"]["content"].strip()
    assert content, "补全内容为空"


# ---------------- TC07 SFT LoRA 训练 + 产物归档 + 血缘 ----------------

def test_tc07_sft_training():
    tok = STATE["admin"]
    # 释放 GPU：先停推理部署（单卡互斥）
    if _deploy().get("status") == "running":
        r = requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/stop",
                          headers=H(tok), timeout=30)
        assert r.status_code == 200, r.text
        poll(lambda: _deploy().get("status") == "stopped" or None,
             timeout=120, desc="等待部署停止")

    r = requests.post(f"{BASE}/api/training/jobs", headers=H(tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT,
        "name": f"e2e-sft-{SUFFIX}",
        "stage": "sft",
        "finetuning_type": "lora",
        "base_model_id": BASE_MODEL_ASSET_ID,
        "dataset_version_ids": [STATE["ver"]["id"]],
        "hyperparams": {
            "cutoff_len": 512,
            "logging_steps": 2,
            "save_steps": 10,
            "per_device_train_batch_size": 1,
            "gradient_accumulation_steps": 1,
            "num_train_epochs": 1,
            "learning_rate": 5e-5,
            "lora_rank": 8,
            "lora_alpha": 16,
        },
        "gpu_count": 1,
        "priority": 5,
    })
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    STATE["job_id"] = jid

    def check():
        j = requests.get(f"{BASE}/api/training/jobs/{jid}",
                         headers=H(tok), timeout=15).json()
        return j if j["status"] in ("succeeded", "failed") else None

    j = poll(check, timeout=1800, interval=10, desc="等待 SFT 训练完成")
    if j["status"] != "succeeded":
        lg = requests.get(f"{BASE}/api/training/jobs/{jid}/logs",
                          headers=H(tok), timeout=15).json()
        pytest.fail(f"训练失败: {j.get('error')}\n--- run.log 尾部 ---\n"
                    f"{str(lg.get('logs', lg))[-2500:]}")

    # 训练指标（loss 曲线）
    r = requests.get(f"{BASE}/api/training/jobs/{jid}/metrics", headers=H(tok), timeout=15)
    assert r.status_code == 200

    # 产物自动归档到模型仓库
    r = requests.get(f"{BASE}/api/models", params={"project_id": DEFAULT_PROJECT},
                     headers=H(tok), timeout=15)
    art = [a for a in r.json() if a.get("job_id") == jid]
    assert art, "训练产物未注册到模型仓库"
    STATE["lora_asset"] = art[0]

    # 血缘：LoRA -> 基座，且关联训练数据集版本
    r = requests.get(f"{BASE}/api/models/{art[0]['id']}/lineage", headers=H(tok), timeout=15)
    assert r.status_code == 200
    lin = r.json()
    chain_ids = [c["id"] for c in lin.get("chain", [])]
    assert len(chain_ids) >= 2 and chain_ids[-1] == BASE_MODEL_ASSET_ID, \
        f"血缘链未指向基座: {lin.get('chain')}"
    dsvs = lin.get("dataset_versions", [])
    assert any(v["id"] == STATE["ver"]["id"] for v in dsvs), \
        f"血缘未关联训练数据集版本: {dsvs}"


# ---------------- TC08 自动化评测（规则匹配） ----------------

def test_tc08_eval():
    tok = STATE["admin"]
    _ensure_deploy_running()

    r = requests.post(f"{BASE}/api/evals", headers=H(tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT,
        "name": f"e2e-eval-{SUFFIX}",
        "target_deployment_id": DEPLOY_ID,
        "dataset_version_id": STATE["ver"]["id"],
        "method": "rule",
        "limit": 3,
    })
    assert r.status_code == 200, r.text
    tid = r.json()["id"]
    STATE["eval_id"] = tid

    def check():
        t = requests.get(f"{BASE}/api/evals/{tid}", headers=H(tok), timeout=15).json()
        return t if t["status"] in ("succeeded", "failed") else None

    t = poll(check, timeout=900, interval=10, desc="等待评测完成")
    if t["status"] != "succeeded":
        pytest.fail(f"评测失败: {t.get('error')}")
    assert t.get("summary"), f"评测 summary 为空: {t}"


# ---------------- TC09 LoRA 热加载 ----------------

def test_tc09_lora_hotload():
    tok = STATE["admin"]
    _ensure_deploy_running()
    aname = f"e2e-{SUFFIX}"

    r = requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/adapters",
                      headers=H(tok), timeout=120,
                      json={"name": aname, "asset_id": STATE["lora_asset"]["id"]})
    assert r.status_code == 200, r.text
    assert r.json().get("ok"), f"热加载失败: {r.text}"

    d = _deploy()
    assert any(a.get("name") == aname for a in (d.get("lora_adapters") or [])), \
        "部署未记录热加载的 adapter"

    # 卸载（清理）
    r = requests.delete(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/adapters/{aname}",
                        headers=H(tok), timeout=60)
    assert r.status_code == 200, r.text


# ---------------- TC10 内容护栏 + 审计日志 ----------------

def test_tc10_guard_audit():
    tok = STATE["admin"]
    secret = f"e2esecret-{SUFFIX}"

    # 创建护栏规则
    r = requests.post(f"{BASE}/api/admin/guard-rules", headers=H(tok), timeout=15,
                      json={"pattern": secret, "scope": "both",
                            "description": "e2e 护栏用例"})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]

    body = {"model": DEPLOY_NAME,
            "messages": [{"role": "user", "content": f"请原样重复: {secret}"}],
            "max_tokens": 8}
    # 命中护栏 -> 451
    r = requests.post(f"{BASE}/v1/{DEPLOY_NAME}/chat/completions",
                      headers=H(tok), json=body, timeout=60)
    assert r.status_code == 451, f"护栏应拦截为 451，实际 {r.status_code}: {r.text[:200]}"

    # 删除规则后放行
    r = requests.delete(f"{BASE}/api/admin/guard-rules/{rid}", headers=H(tok), timeout=15)
    assert r.status_code == 200
    r = requests.post(f"{BASE}/v1/{DEPLOY_NAME}/chat/completions",
                      headers=H(tok), json=body, timeout=60)
    assert r.status_code == 200, f"规则删除后应放行: {r.text[:200]}"

    # 审计日志：护栏规则创建（admin 写操作）与 TC03 中开发者的 403 都应留痕
    r = requests.get(f"{BASE}/api/admin/audit",
                     params={"username": "admin", "limit": 300},
                     headers=H(tok), timeout=15)
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["path"].endswith("/api/admin/guard-rules") and i["method"] == "POST"
               for i in items), "审计日志缺少护栏规则创建记录"
    r = requests.get(f"{BASE}/api/admin/audit",
                     params={"username": STATE["dev"][0], "limit": 100},
                     headers=H(tok), timeout=15)
    dev_items = r.json()["items"]
    assert any(i["status_code"] == 403 for i in dev_items), "审计日志缺少开发者的 403 拒绝记录"


# ---------------- TC11-13 训练场景 × 高级参数（真实 GPU 全链路） ----------------
# 与 TC07 的差异：TC07 只覆盖基础超参（lr/epochs/lora_rank 等）；本节覆盖
# ① extra_args JSON 透传（最高优先级注入通道）② deepspeed ZeRO-2 多卡 ③
# 精度/优化器/调度器/lora 组高级超参 ④ finetuning_type=full 分支 ⑤ export 合并
# 高级参数。关键断言逻辑：LLaMA-Factory 的 HfArgumentParser 遇到不支持/拼错的
# 键会直接使任务失败（"Some specified arguments were not used"），因此
# "任务 succeeded" 本身即证明所有高级参数被训练框架真实接受。

ADV_HP = {
    # 基础控制（小样本快速跑完）
    "cutoff_len": 256,
    "per_device_train_batch_size": 1,
    "gradient_accumulation_steps": 1,
    "num_train_epochs": 1,
    "logging_steps": 2,
    "save_steps": 10,
    # 高级超参（train / lora / precision 组）
    "learning_rate": 5e-5,
    "lora_rank": 8,
    "lora_alpha": 16,
    "lora_dropout": 0.05,
    "compute_type": "pure_bf16",   # 精度组：派生 bf16+pure_bf16
    "optim": "adamw_torch",        # 训练组：优化器
    "lr_scheduler_type": "cosine", # 训练组：调度器
    "warmup_ratio": 0.05,          # 训练组
    "weight_decay": 0.01,          # 训练组
    # extra_args：用户 JSON 透传（最高优先级）。neftune 为 LF 真实高级参数，
    # 走透传通道注入；任务成功即证明其被 LLaMA-Factory 接受
    "extra_args": json.dumps({"neftune_noise_alpha": 5.0}),
}


def _ensure_dataset_version() -> int:
    """复用 TC05 的数据集版本；独立运行本组用例（-k）时现造最小 alpaca 数据集。"""
    if STATE.get("ver"):
        return STATE["ver"]["id"]
    tok = STATE["admin"]
    r = requests.post(f"{BASE}/api/datasets",
                      json={"project_id": DEFAULT_PROJECT,
                            "name": f"e2e-adv-ds-{SUFFIX}"},
                      headers=H(tok), timeout=15)
    r.raise_for_status()
    dsid = r.json()["id"]
    files = {"file": ("samples.json",
                      json.dumps(SAMPLES, ensure_ascii=False).encode("utf-8"),
                      "application/json")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      params={"fmt": "alpaca"}, files=files,
                      headers=H(tok), timeout=30)
    r.raise_for_status()
    STATE["ver"] = r.json()
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/{STATE['ver']['id']}/commit",
                      headers=H(tok), timeout=60)
    r.raise_for_status()
    return STATE["ver"]["id"]


def _wait_job(jid: int, timeout: float = 1800) -> dict:
    def check():
        j = requests.get(f"{BASE}/api/training/jobs/{jid}",
                         headers=H(STATE["admin"]), timeout=15).json()
        return j if j["status"] in ("succeeded", "failed") else None
    return poll(check, timeout=timeout, interval=10, desc=f"训练任务 {jid} 完成")


def _assert_job_ok(jid: int, j: dict):
    if j["status"] != "succeeded":
        lg = requests.get(f"{BASE}/api/training/jobs/{jid}/logs",
                          headers=H(STATE["admin"]), timeout=15).json()
        pytest.fail(f"训练失败: {j.get('error')}\n--- run.log 尾部 ---\n"
                    f"{str(lg.get('logs', lg))[-2500:]}")


def _wait_artifact(jid: int, timeout: float = 180):
    """轮询任务产物归档到模型仓库（归档可能滞后于 succeeded）"""
    def check():
        r = requests.get(f"{BASE}/api/models",
                         params={"project_id": DEFAULT_PROJECT},
                         headers=H(STATE["admin"]), timeout=15)
        art = [a for a in r.json() if a.get("job_id") == jid]
        return art[0] if art else None
    return poll(check, timeout=timeout, interval=5, desc=f"任务 {jid} 产物归档")


def test_tc11_sft_lora_2gpu_zer2_advanced():
    """SFT+LoRA + 2 卡 DeepSpeed ZeRO-2 + 高级超参（精度/优化器/调度器/lora 组
    /extra_args 透传 neftune）。先停常驻部署释放双卡（TC13 末尾恢复）。"""
    tok = STATE["admin"]
    if _deploy().get("status") == "running":
        requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/stop",
                      headers=H(tok), timeout=30).raise_for_status()
        poll(lambda: _deploy().get("status") == "stopped" or None,
             timeout=120, desc="等待部署停止")

    r = requests.post(f"{BASE}/api/training/jobs", headers=H(tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT,
        "name": f"e2e-adv-lora-{SUFFIX}",
        "stage": "sft",
        "finetuning_type": "lora",
        "base_model_id": BASE_MODEL_ASSET_ID,
        "dataset_version_ids": [_ensure_dataset_version()],
        "hyperparams": dict(ADV_HP),
        "deepspeed": "zero2",
        "gpu_count": 2,
        "priority": 5,
    })
    assert r.status_code == 200, r.text
    jid = r.json()["id"]

    _assert_job_ok(jid, _wait_job(jid))

    r = requests.get(f"{BASE}/api/training/jobs/{jid}/metrics",
                     headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text

    art = _wait_artifact(jid)
    assert art, "训练产物未注册到模型仓库"
    STATE["adv_lora_asset"] = art


def test_tc12_sft_full_advanced():
    """SFT + 全参微调（finetuning_type=full 分支，TC07 未覆盖）+ 高级超参。
    0.5B 全参单卡即可；验证无 lora 组的配置路径 + 精度/优化器/调度器高级参数。"""
    tok = STATE["admin"]
    hp = {k: v for k, v in ADV_HP.items()
          if k not in ("lora_rank", "lora_alpha", "lora_dropout")}
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT,
        "name": f"e2e-adv-full-{SUFFIX}",
        "stage": "sft",
        "finetuning_type": "full",
        "base_model_id": BASE_MODEL_ASSET_ID,
        "dataset_version_ids": [_ensure_dataset_version()],
        "hyperparams": hp,
        "deepspeed": "none",
        "gpu_count": 1,
        "priority": 5,
    })
    assert r.status_code == 200, r.text
    jid = r.json()["id"]

    _assert_job_ok(jid, _wait_job(jid, timeout=2400))

    r = requests.get(f"{BASE}/api/training/jobs/{jid}/metrics",
                     headers=H(tok), timeout=15)
    assert r.status_code == 200, r.text

    art = _wait_artifact(jid, timeout=300)
    assert art, "全参训练产物未注册到模型仓库"
    STATE["adv_full_asset"] = art


def test_tc13_export_merge_advanced():
    """export 阶段：合并 LoRA → 完整模型 + export 组高级参数
    （export_size/export_device=cpu）。复用 TC11 的 LoRA 产物（独立运行时
    回退 TC07 产物或仓库中现成 LoRA）。完成后恢复常驻部署 running。"""
    tok = STATE["admin"]
    adapter = STATE.get("adv_lora_asset") or STATE.get("lora_asset")
    if not adapter:
        r = requests.get(f"{BASE}/api/models",
                         params={"project_id": DEFAULT_PROJECT, "mtype": "lora"},
                         headers=H(tok), timeout=15)
        # 取真正含 adapter_config.json 的 LoRA（历史全参检查点曾被误标 lora，
        # 误取会让 export 加载 adapter 失败）
        adapter = next((a for a in r.json()
                        if os.path.exists(f"{a.get('path', '')}/adapter_config.json")),
                       None)
    if not adapter:
        pytest.skip("无可用 LoRA 产物，跳过 export 场景")
    try:
        r = requests.post(f"{BASE}/api/training/jobs", headers=H(tok),
                          timeout=30, json={
                              "project_id": DEFAULT_PROJECT,
                              "name": f"e2e-adv-export-{SUFFIX}",
                              "stage": "export",
                              "finetuning_type": "lora",
                              "base_model_id": BASE_MODEL_ASSET_ID,
                              "adapter_asset_id": adapter["id"],
                              "hyperparams": {"export_size": 1,
                                              "export_device": "cpu"},
                              "gpu_count": 1,
                              "priority": 5,
                          })
        assert r.status_code == 200, r.text
        jid = r.json()["id"]

        _assert_job_ok(jid, _wait_job(jid, timeout=1200))

        # 合并模型：已注册、体积与基座同量级（0.5B ≈ 1GB，绝非空壳）
        art = _wait_artifact(jid, timeout=300)
        assert art, "合并模型产物未注册到模型仓库"
        assert (art.get("size_bytes") or 0) > 50_000_000, \
            f"合并模型体积异常（应≈1GB）: {art.get('size_bytes')}"
    finally:
        # 恢复常驻部署（TC11 停掉了），保持环境状态与测试前一致
        d = _deploy()
        if d.get("status") != "running":
            requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/start",
                          headers=H(tok), timeout=30)
            _wait_deploy_ready(timeout=900)

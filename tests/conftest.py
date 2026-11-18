# -*- coding: utf-8 -*-
"""自动化测试套件共享 fixture（一级目录 tests/，2026-11-16 由 platform/backend/tests/ 迁入）。

运行环境：backend 容器内（pytest 直连本进程 3001 端口，与 test_e2e.py 相同）。
本套件为"快速集成 + 单测"套件：不执行真实 GPU 训练；仅包含少量受控的真实任务
（清洗/合成/评测 1 次、部署生命周期 1 次、MinerU 解析 1 次）。

所有测试实体带唯一后缀 SUF，可重复运行。
"""
import os
import sys
import time
import uuid

# 单测文件需导入 app 包：容器内 /app，本地仓库 platform/backend
_HERE = os.path.dirname(os.path.abspath(__file__))
for _p in ("/app", os.path.abspath(os.path.join(_HERE, "..", "platform", "backend"))):
    if os.path.isdir(os.path.join(_p, "app")) and _p not in sys.path:
        sys.path.insert(0, _p)

import pytest
import requests

BASE = "http://127.0.0.1:3001"
SUF = uuid.uuid4().hex[:6]

DEPLOY_NAME = "qwen25-05b-base"


def retry_call(fn, tries: int = 3, delay: float = 15.0, desc: str = ""):
    """整体重试工具（flaky 对策，不引入 pytest-rerunfailures 依赖）。

    环境型瞬态失败（并发改动窗口 / 上游 LLM 偶发超时）下，整段重跑而非断言失败。
    每次尝试应创建全新实体（带 SUF 后缀天然唯一），互不污染。
    """
    last = None
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001 —— 重试即吞掉本次异常
            last = e
            if i < tries - 1:
                time.sleep(delay)
    raise AssertionError(f"[{desc or fn.__name__}] {tries} 次尝试均失败，最后异常: {last}")


def login(username: str, password: str) -> str:
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": username, "password": password}, timeout=15)
    r.raise_for_status()
    return r.json()["token"]


def H(tok: str) -> dict:
    return {"Authorization": f"Bearer {tok}"}


def _discover_env() -> tuple[int, int, int]:
    """动态发现 默认项目 / ready 基座资产 / 常驻部署 的 ID。

    全新部署（deploy.sh）后自动种子为：default 租户 + default-project 项目，
    基座/部署 ID 取决于初始化顺序——因此测试不硬编码任何 ID，任何环境可跑。
    前提：已完成基座初始化（POST /api/models/base 且 ready + 创建常驻部署）。
    """
    tok = login("admin", "admin123")
    r = requests.get(f"{BASE}/api/admin/projects", headers=H(tok), timeout=15)
    r.raise_for_status()
    projs = r.json()
    proj = next((p for p in projs if p["name"] == "default-project"), projs[0])
    dp = proj["id"]

    r = requests.get(f"{BASE}/api/models", params={"project_id": dp, "mtype": "base"},
                     headers=H(tok), timeout=15)
    r.raise_for_status()
    rows = r.json()
    ready = [a for a in rows if a["status"] == "ready"]
    # API 测试套件会在默认项目注册 1 字节假模型（500m* 前缀），按体积过滤出真实
    # 基座（真实模型 ≥ 数百 MB；假资产 size_bytes ≤ 几 KB），防止误选假资产训练
    real = [a for a in ready if (a.get("size_bytes") or 0) >= 10_000_000]
    pick = real or ready or rows
    # 全参训练产物也注册为 base 型（完整模型，本就可再作基座）；优先选无血缘的
    # 「根基座」，避免把中间微调产物当测试基座（血缘链断言会因此多出一级）
    roots = [a for a in pick if not a.get("parent_name")]
    if roots:
        pick = roots
    if not pick:
        raise RuntimeError("默认项目缺少基座模型资产，请先完成基座初始化"
                           "（POST /api/models/base 并等待下载 ready）")
    asset = pick[0]["id"]

    r = requests.get(f"{BASE}/api/inference/deployments", params={"project_id": dp},
                     headers=H(tok), timeout=15)
    r.raise_for_status()
    deps = r.json()
    if not deps:
        raise RuntimeError("默认项目缺少推理部署，请先创建常驻部署"
                           f"（POST /api/inference/deployments，name={DEPLOY_NAME}）")
    row = next((d for d in deps if d["name"] == DEPLOY_NAME), deps[0])
    if not row.get("enable_lora"):
        raise RuntimeError(f"常驻部署 {DEPLOY_NAME} 缺少 enable_lora=true"
                           "（LoRA 热加载用例依赖 vLLM --enable-lora），请重建常驻部署时带上该参数")
    return dp, asset, row["id"]


DEFAULT_PROJECT, BASE_MODEL_ASSET_ID, DEPLOY_ID = _discover_env()


def poll(fn, timeout: float = 120.0, interval: float = 2.0, desc: str = ""):
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        last = fn()
        if last:
            return last
        time.sleep(interval)
    raise AssertionError(f"{desc} 超时({timeout:.0f}s)，最后状态: {last}")


# ---------------- 会话级 fixture ----------------

@pytest.fixture(scope="session")
def admin_tok() -> str:
    return login("admin", "admin123")


def _mk_tenant(admin: str, tag: str) -> dict:
    """创建 租户 + 项目 + 租户管理员 + 开发者 + 只读，开发者/只读加入项目。"""
    t = requests.post(f"{BASE}/api/admin/tenants", headers=H(admin), timeout=15,
                      json={"name": f"t500-{tag}-{SUF}"})
    t.raise_for_status()
    tid = t.json()["id"]
    p = requests.post(f"{BASE}/api/admin/projects", headers=H(admin), timeout=15,
                      json={"tenant_id": tid, "name": f"p500-{tag}-{SUF}"})
    p.raise_for_status()
    pid = p.json()["id"]

    def mk_user(role: str) -> tuple[str, int, str]:
        uname = f"u500-{tag}-{role[:3]}-{SUF}"
        r = requests.post(f"{BASE}/api/admin/users", headers=H(admin), timeout=15,
                          json={"username": uname, "password": "pass12345",
                                "role": role, "tenant_id": tid})
        r.raise_for_status()
        uid = r.json()["id"]
        if role in ("developer", "viewer"):
            m = requests.put(f"{BASE}/api/admin/projects/{pid}/members", headers=H(admin),
                             timeout=15, json={"user_id": uid, "role": role})
            m.raise_for_status()
        return uname, uid, login(uname, "pass12345")

    ta_name, ta_uid, ta_tok = mk_user("tenant_admin")
    dev_name, dev_uid, dev_tok = mk_user("developer")
    vw_name, vw_uid, vw_tok = mk_user("viewer")
    return {"tenant_id": tid, "proj_id": pid,
            "ta": (ta_name, ta_uid, ta_tok),
            "dev": (dev_name, dev_uid, dev_tok),
            "viewer": (vw_name, vw_uid, vw_tok)}


@pytest.fixture(scope="session")
def tenant_a(admin_tok) -> dict:
    return _mk_tenant(admin_tok, "A")


@pytest.fixture(scope="session")
def tenant_b(admin_tok) -> dict:
    return _mk_tenant(admin_tok, "B")


@pytest.fixture(scope="session")
def roles(admin_tok, tenant_a) -> dict:
    """四种角色的 token（RBAC 矩阵用）"""
    return {
        "super_admin": admin_tok,
        "tenant_admin": tenant_a["ta"][2],
        "developer": tenant_a["dev"][2],
        "viewer": tenant_a["viewer"][2],
    }


@pytest.fixture(scope="session")
def api_key(admin_tok) -> str:
    """默认项目的 x-api-key 原值（仅展示一次）"""
    r = requests.post(f"{BASE}/api/admin/projects/{DEFAULT_PROJECT}/api-keys",
                      headers=H(admin_tok), timeout=15,
                      json={"name": f"500-{SUF}"})
    r.raise_for_status()
    return r.json()["key"]


ALPACA_ROWS = [
    {"instruction": "1+1 等于几？只回答数字。", "input": "", "output": "2"},
    {"instruction": "水的化学式是什么？", "input": "", "output": "H2O"},
    {"instruction": "我的手机号 13812345678，邮箱 test01@example.com，请记录。",
     "input": "", "output": "已记录"},
    {"instruction": "身份证号 110101199003077758，银行卡 6222020200112233445。",
     "input": "", "output": "已登记"},
    {"instruction": "1+1 等于几？只回答数字。", "input": "", "output": "2"},  # 与第 1 条重复
    {"instruction": "A", "input": "", "output": "B"},                         # 短样本
]

DPO_ROWS = [
    {"prompt": "把 hello 翻译成中文", "chosen": "你好", "rejected": "哈喽世界"},
    {"prompt": "把 thanks 翻译成中文", "chosen": "谢谢", "rejected": "多谢多謝"},
    {"prompt": "", "chosen": "x", "rejected": "y"},  # 无 prompt，应被过滤
]


@pytest.fixture(scope="session")
def dsctx(admin_tok) -> dict:
    """默认项目：alpaca 数据集(含 PII/重复/短样本) + dpo 数据集，各 1 个版本"""
    out = {}
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT,
                            "name": f"500alpaca-{SUF}", "description": "500 套件"})
    r.raise_for_status()
    dsid = r.json()["id"]
    files = {"file": ("rows.jsonl",
                      "\n".join(__import__("json").dumps(x, ensure_ascii=False) for x in ALPACA_ROWS),
                      "application/octet-stream")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "alpaca", "note": "v1"}, files=files)
    r.raise_for_status()
    out["alpaca_ds"] = dsid
    out["alpaca_ver"] = r.json()["id"]

    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500dpo-{SUF}"})
    r.raise_for_status()
    dsid2 = r.json()["id"]
    files = {"file": ("dpo.jsonl",
                      "\n".join(__import__("json").dumps(x, ensure_ascii=False) for x in DPO_ROWS),
                      "application/octet-stream")}
    r = requests.post(f"{BASE}/api/datasets/{dsid2}/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "dpo"}, files=files)
    r.raise_for_status()
    out["dpo_ds"] = dsid2
    out["dpo_ver"] = r.json()["id"]
    return out


@pytest.fixture(scope="session")
def proj_a_assets(admin_tok, tenant_a) -> dict:
    """租户 A 项目内的完整上下文：ready 基座（磁盘已存在）+ 数据集版本 + 排队训练任务"""
    import json as _json
    out = {}
    # 1) ready 基座：backend 容器内直接建目录（/data 已挂载），注册即 ready
    bname = f"500baseA-{SUF}"
    bpath = f"/data/models/hub/{bname}"
    os.makedirs(bpath, exist_ok=True)
    with open(os.path.join(bpath, "README.md"), "w") as f:
        f.write("placeholder base for 500-suite\n")
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": tenant_a["proj_id"], "name": bname,
                            "hf_id": "local/placeholder"})
    r.raise_for_status()
    a = r.json()
    assert a["status"] == "ready", f"本地占位基座应直接 ready: {a['status']}"
    out["base_asset_id"] = a["id"]
    out["base_name"] = bname
    # 2) 数据集 + 版本
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": tenant_a["proj_id"], "name": f"500dsA-{SUF}"})
    r.raise_for_status()
    dsid = r.json()["id"]
    rows = [{"instruction": "问题甲", "input": "", "output": "答案甲"},
            {"instruction": "问题乙", "input": "", "output": "答案乙"}]
    files = {"file": ("a.jsonl", "\n".join(_json.dumps(x, ensure_ascii=False) for x in rows),
                      "application/octet-stream")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "alpaca"}, files=files)
    r.raise_for_status()
    out["ds_id"] = dsid
    out["ver_id"] = r.json()["id"]
    # 3) 排队训练任务（GPU 被部署 1 占用，将保持 queued）
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=30, json={
        "project_id": tenant_a["proj_id"], "name": f"500jobA-{SUF}", "stage": "sft",
        "finetuning_type": "lora", "base_model_id": a["id"],
        "dataset_version_ids": [out["ver_id"]], "gpu_count": 1, "priority": 5,
        "hyperparams": {"cutoff_len": 512, "num_train_epochs": 1}})
    r.raise_for_status()
    out["job_id"] = r.json()["id"]
    return out


@pytest.fixture(scope="session")
def succeeded_ckpt(admin_tok):
    """找一个有 checkpoint 的成功训练任务（默认项目），供产物注册/热加载用；无则 None"""
    r = requests.get(f"{BASE}/api/training/jobs",
                     params={"project_id": DEFAULT_PROJECT, "status": "succeeded"},
                     headers=H(admin_tok), timeout=15)
    for j in r.json():
        detail = requests.get(f"{BASE}/api/training/jobs/{j['id']}",
                              headers=H(admin_tok), timeout=15).json()
        if detail.get("checkpoints"):
            return {"job_id": j["id"], "ckpt_path": detail["checkpoints"][0]["path"]}
    return None


@pytest.fixture(scope="session")
def base_asset_name(admin_tok) -> str:
    """默认项目中 ready 状态的基座资产名（部署/血缘测试用）"""
    r = requests.get(f"{BASE}/api/models", params={"project_id": DEFAULT_PROJECT},
                     headers=H(admin_tok), timeout=15)
    rows = [a for a in r.json() if a["status"] == "ready" and a["type"] == "base"]
    assert rows, "默认项目缺少 ready 基座模型"
    return next((a["name"] for a in rows if a["id"] == BASE_MODEL_ASSET_ID), rows[0]["name"])

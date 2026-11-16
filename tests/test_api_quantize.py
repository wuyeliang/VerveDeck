# -*- coding: utf-8 -*-
"""模型压缩（量化）中心：端到端测试用例

覆盖量化模块全链路（方法元数据 → 创建校验 → 列表/详情/日志 → 删除规则 →
调度器状态机 → 产物注册入仓库 → 产物保护）：

A. 量化方式（GET /methods）：4 种方式元数据、未登录 401
B. 创建校验（POST）：4 种方式可创建 / 非法方式 / 资产不存在 / 跨项目资产 /
   未就绪 / LoRA 未合并 / 同资产并发任务 / 跨租户 / 未登录
C. 列表与查询（GET）：项目过滤 / 摘要字段 / 租户隔离 / 详情 / 日志 / 404 / 跨租户
D. 删除规则（DELETE）：进行中不可删 / 失败任务可删且清理任务目录 / 不存在 /
   跨租户 / 已注册产物的任务不可删（保护模型仓库中的量化产物）
E. 调度器状态机：
   - running + exec 上下文丢失 → 真调度器（backend 内线程）收敛为 failed + quant_failed 告警
   - _finalize_quant 成功 → 量化产物注册为 merged 资产（命名 quant-Nb-*、血缘 parent）
   - 成功但产物缺失 → failed（未找到产物）
   - _finalize_quant 失败 → failed + 告警事件
F. 服务级：build_config 4 种方式全字段 / task_dir / register_artifact 重名去重 / 空产物 None

前提（与 conftest 一致）：3001 存活 backend + 基座已初始化 + 常驻部署占用 GPU；
本文件创建的量化任务保持 pending，不真正占卡。
E/F 的进程内部分直接 import app，与 backend 进程共享 DB 与 data_dir
（容器内运行天然满足；本地验证需保持 DB_URL/DATA_DIR 环境变量一致）。
"""
import os
import shutil

import pytest
import requests

from conftest import BASE, H, SUF, poll, DEFAULT_PROJECT


def _data_dir() -> str:
    return os.environ.get("DATA_DIR", "/data")


def _task(admin_tok: str, tid: int) -> dict:
    r = requests.get(f"{BASE}/api/quantize/{tid}", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200, f"获取任务 {tid} 失败: {r.status_code} {r.text[:200]}"
    return r.json()


def _is_terminal(t: dict) -> bool:
    return t["status"] in ("succeeded", "failed")


def _mk_base(admin_tok: str, project_id: int, tag: str) -> dict:
    """占位基座：本地目录直接 ready（与 test_api_training 的占位资产同套路）"""
    name = f"500q{tag}-{SUF}"
    path = f"{_data_dir()}/models/hub/{name}"
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "README.md"), "w") as f:
        f.write(f"placeholder base for quant e2e {name}\n")
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": project_id, "name": name,
                            "hf_id": "local/placeholder"})
    r.raise_for_status()
    a = r.json()
    assert a["status"] == "ready", f"占位基座应直接 ready: {a['status']}"
    return a


def _mk_lora(admin_tok: str, project_id: int, tag: str) -> dict:
    name = f"500qlora{tag}-{SUF}"
    path = f"{_data_dir()}/models/hub/{name}"
    os.makedirs(path, exist_ok=True)
    with open(os.path.join(path, "adapter_model.safetensors"), "w") as f:
        f.write("placeholder lora")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": project_id, "name": name,
                            "type": "lora", "path": path})
    r.raise_for_status()
    return r.json()


def _create_task(admin_tok: str, project_id: int, asset_id: int, method: str,
                 name: str) -> dict:
    r = requests.post(f"{BASE}/api/quantize", headers=H(admin_tok), timeout=30,
                      json={"project_id": project_id, "name": name,
                            "model_asset_id": asset_id, "method": method})
    r.raise_for_status()
    return r.json()


@pytest.fixture(scope="module")
def qctx(admin_tok):
    """默认项目：4 个 ready 占位基座（每方式一个）+ 未就绪基座 + LoRA 产物 + 4 个 pending 任务"""
    c = {}
    for m in ("bnb4", "bnb8", "gptq4", "awq4"):
        c[f"asset_{m}"] = _mk_base(admin_tok, DEFAULT_PROJECT, m)
    # 未就绪基座（pending_download）
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT,
                            "name": f"500qpend-{SUF}", "hf_id": "org/none"})
    r.raise_for_status()
    c["pending_asset"] = r.json()
    assert c["pending_asset"]["status"] == "pending_download"
    c["lora_asset"] = _mk_lora(admin_tok, DEFAULT_PROJECT, "")
    for m in ("bnb4", "bnb8", "gptq4", "awq4"):
        c[f"task_{m}"] = _create_task(admin_tok, DEFAULT_PROJECT,
                                      c[f"asset_{m}"]["id"], m, f"500qt-{m}-{SUF}")["id"]
    return c


# ---------- E/F 共用的进程内工具（与 backend 共享 DB / data_dir） ----------

def _db():
    from app.database import SessionLocal
    return SessionLocal()


def _quant_task_dir(tid: int) -> str:
    from app.services import quantize_service as qs
    return qs.task_dir(tid)


def _fake_out(tid: int) -> str:
    """预置假量化产物（config.json + 权重文件），模拟 llamafactory export 成功输出"""
    out = os.path.join(_quant_task_dir(tid), "out")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "config.json"), "w") as f:
        f.write('{"quantization_config": {"bits": 4}}')
    with open(os.path.join(out, "model.safetensors"), "wb") as f:
        f.write(b"fake-quantized-weights")
    return out


def _finalize(tid: int, success: bool, error: str = ""):
    """进程内调用调度器收敛函数，把任务推到终态（不依赖 docker）。
    不经过 running 中间态：避免与 backend 进程内真调度器的 tick 竞态。"""
    from app import scheduler
    from app.models import QuantTask
    db = _db()
    try:
        t = db.get(QuantTask, tid)
        assert t is not None
        scheduler._finalize_quant(db, t, success, error)
        db.refresh(t)
        return {"status": t.status, "error": t.error, "finished_at": t.finished_at}
    finally:
        db.close()


# ================= A. 量化方式元数据 =================

def test_methods_list(admin_tok):
    r = requests.get(f"{BASE}/api/quantize/methods", headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    rows = r.json()
    assert {m["key"] for m in rows} == {"bnb4", "bnb8", "gptq4", "awq4"}
    by = {m["key"]: m for m in rows}
    assert by["bnb4"]["bit"] == 4 and by["bnb4"]["method"] == "bitsandbytes"
    assert by["bnb4"]["device"] == "cpu"
    assert by["bnb8"]["bit"] == 8 and by["bnb8"]["method"] == "bitsandbytes"
    assert by["gptq4"]["bit"] == 4 and by["gptq4"]["method"] == "gptq"
    assert by["awq4"]["bit"] == 4 and by["awq4"]["method"] == "awq"
    assert all(m["label"] for m in rows)


def test_methods_requires_auth():
    r = requests.get(f"{BASE}/api/quantize/methods", timeout=15)
    assert r.status_code == 401


# ================= B. 创建校验 =================

def test_create_all_methods_accepted(qctx, admin_tok):
    for m in ("bnb4", "bnb8", "gptq4", "awq4"):
        t = _task(admin_tok, qctx[f"task_{m}"])
        assert t["method"] == m
        assert t["gpu_count"] == 1, "量化任务固定 1 卡"
        assert t["status"] in ("pending", "running")
        assert t["method_label"], "method_label 缺失"
        assert t["model_name"] == qctx[f"asset_{m}"]["name"]


def test_create_bad_method_400(qctx, admin_tok):
    r = requests.post(f"{BASE}/api/quantize", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500qbm-{SUF}",
                            "model_asset_id": qctx["asset_bnb4"]["id"],
                            "method": "fp8bogus"})
    assert r.status_code == 400


def test_create_missing_asset_404(qctx, admin_tok):
    r = requests.post(f"{BASE}/api/quantize", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500qma-{SUF}",
                            "model_asset_id": 999999, "method": "bnb4"})
    assert r.status_code == 404


def test_create_cross_project_asset_404(qctx, admin_tok, tenant_a):
    """他项目资产不能用于本项目量化（应 404 而非误用）"""
    other_asset = _mk_base(admin_tok, tenant_a["proj_id"], "other")
    r = requests.post(f"{BASE}/api/quantize", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500qcp-{SUF}",
                            "model_asset_id": other_asset["id"], "method": "bnb4"})
    assert r.status_code == 404


def test_create_not_ready_400(qctx, admin_tok):
    r = requests.post(f"{BASE}/api/quantize", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500qnr-{SUF}",
                            "model_asset_id": qctx["pending_asset"]["id"], "method": "bnb4"})
    assert r.status_code == 400
    assert "尚未就绪" in r.json()["detail"]


def test_create_lora_400(qctx, admin_tok):
    r = requests.post(f"{BASE}/api/quantize", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500qlr-{SUF}",
                            "model_asset_id": qctx["lora_asset"]["id"], "method": "bnb4"})
    assert r.status_code == 400
    assert "合并" in r.json()["detail"]


def test_create_busy_400(qctx, admin_tok):
    """同资产已有 pending/running 任务时，重复创建应被拒且提示任务号"""
    first = _task(admin_tok, qctx["task_bnb4"])
    if _is_terminal(first):
        pytest.skip("环境存在空闲 GPU，首个任务已收敛终态，无并发可测")
    r = requests.post(f"{BASE}/api/quantize", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500qbusy-{SUF}",
                            "model_asset_id": qctx["asset_bnb4"]["id"], "method": "bnb4"})
    assert r.status_code == 400
    assert str(qctx["task_bnb4"]) in r.json()["detail"]


def test_create_cross_tenant_403(qctx, admin_tok, tenant_b):
    _, _, dev_tok = tenant_b["dev"]
    r = requests.post(f"{BASE}/api/quantize", headers=H(dev_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500qct-{SUF}",
                            "model_asset_id": qctx["asset_bnb4"]["id"], "method": "bnb4"})
    assert r.status_code == 403


def test_create_requires_auth(qctx):
    r = requests.post(f"{BASE}/api/quantize", timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500qna-{SUF}",
                            "model_asset_id": qctx["asset_bnb4"]["id"], "method": "bnb4"})
    assert r.status_code == 401


# ================= C. 列表与查询 =================

def test_list_project_filter(qctx, admin_tok):
    rows = requests.get(f"{BASE}/api/quantize",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    assert rows, "默认项目应能查到本模块创建的任务"
    assert all(t["project_id"] == DEFAULT_PROJECT for t in rows)
    ids = [t["id"] for t in rows]
    assert qctx["task_bnb4"] in ids


def test_list_brief_fields(qctx, admin_tok):
    rows = requests.get(f"{BASE}/api/quantize",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    t = next(x for x in rows if x["id"] == qctx["task_bnb4"])
    for k in ("id", "project_id", "name", "model_asset_id", "model_name",
              "method", "method_label", "gpu_count", "status", "created_at"):
        assert k in t, f"列表摘要缺字段 {k}"


def test_list_tenant_isolation(qctx, tenant_b):
    _, _, dev_tok = tenant_b["dev"]
    # 显式指定他租户项目 → 403
    r = requests.get(f"{BASE}/api/quantize",
                     params={"project_id": DEFAULT_PROJECT},
                     headers=H(dev_tok), timeout=15)
    assert r.status_code == 403
    # 不带过滤：只应看到自己有权项目的任务
    rows = requests.get(f"{BASE}/api/quantize", headers=H(dev_tok), timeout=15).json()
    assert all(t["project_id"] != DEFAULT_PROJECT for t in rows)


def test_get_task(qctx, admin_tok):
    t = _task(admin_tok, qctx["task_bnb4"])
    assert t["name"] == f"500qt-bnb4-{SUF}"
    assert t["method"] == "bnb4"
    assert t["model_asset_id"] == qctx["asset_bnb4"]["id"]


def test_get_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/quantize/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_get_cross_tenant_403(qctx, tenant_b):
    _, _, dev_tok = tenant_b["dev"]
    r = requests.get(f"{BASE}/api/quantize/{qctx['task_bnb4']}", headers=H(dev_tok), timeout=15)
    assert r.status_code == 403


def test_task_logs(qctx, admin_tok):
    r = requests.get(f"{BASE}/api/quantize/{qctx['task_bnb4']}/logs",
                     headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    body = r.json()
    assert isinstance(body["logs"], str)
    assert body["status"] in ("pending", "running", "succeeded", "failed")


def test_logs_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/quantize/999999/logs", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


# ================= D. 删除规则 =================

def test_delete_in_progress_400(qctx, admin_tok):
    t = _task(admin_tok, qctx["task_bnb8"])
    if _is_terminal(t):
        pytest.skip("环境存在空闲 GPU，任务已收敛终态")
    r = requests.delete(f"{BASE}/api/quantize/{qctx['task_bnb8']}",
                        headers=H(admin_tok), timeout=15)
    assert r.status_code == 400


def test_delete_terminal_200_and_dir_removed(qctx, admin_tok):
    """失败任务（无产物）可删，删除后任务目录清理、再查 404"""
    tid = qctx["task_gptq4"]
    res = _finalize(tid, False, "exit code 1\nfake failure for e2e")
    assert res["status"] == "failed"
    r = requests.delete(f"{BASE}/api/quantize/{tid}", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200, f"终态任务应可删除: {r.status_code} {r.text[:200]}"
    assert not os.path.exists(_quant_task_dir(tid)), "任务目录未清理"
    r2 = requests.get(f"{BASE}/api/quantize/{tid}", headers=H(admin_tok), timeout=15)
    assert r2.status_code == 404


def test_delete_registered_artifact_protected(qctx, admin_tok):
    """成功任务产物已注册进模型仓库 → 删任务必须被拒（否则连带 rmtree 量化权重）"""
    tid = qctx["task_awq4"]
    out = _fake_out(tid)
    res = _finalize(tid, True)
    assert res["status"] == "succeeded"
    # 产物应已注册进模型仓库（merged，路径指向 out 目录）
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    arts = [a for a in rows if a.get("path") == out]
    assert arts, "量化产物未注册进模型仓库"
    r = requests.delete(f"{BASE}/api/quantize/{tid}", headers=H(admin_tok), timeout=15)
    assert r.status_code == 400, "已注册产物的任务可删除会破坏模型仓库资产"
    assert "模型资产" in r.json()["detail"]
    assert os.path.exists(out), "产物目录被误删"


def test_delete_missing_404(admin_tok):
    r = requests.delete(f"{BASE}/api/quantize/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_delete_cross_tenant_403(qctx, tenant_b):
    _, _, dev_tok = tenant_b["dev"]
    r = requests.delete(f"{BASE}/api/quantize/{qctx['task_bnb4']}",
                        headers=H(dev_tok), timeout=15)
    assert r.status_code == 403


# ================= D2. 批量删除 =================

def test_batch_delete_mixed(qctx, admin_tok):
    """批量删除混合场景：可删终态成功 / 进行中跳过 / 已注册产物保护 / 不存在报错，
    逐项返回部分结果（deleted + failed），失败项不阻断其他项"""
    # T1：新任务 → 失败终态（可删）
    a1 = _mk_base(admin_tok, DEFAULT_PROJECT, "bd1")
    t1 = _create_task(admin_tok, DEFAULT_PROJECT, a1["id"], "bnb4", f"500qbd1-{SUF}")["id"]
    _finalize(t1, False, "exit code 1\nbatch case")
    # T2：新任务 → 保持 pending（进行中，不可删）
    a2 = _mk_base(admin_tok, DEFAULT_PROJECT, "bd2")
    t2 = _create_task(admin_tok, DEFAULT_PROJECT, a2["id"], "bnb4", f"500qbd2-{SUF}")["id"]
    # T3：新任务 → 成功且产物已注册（受保护，不可删）
    a3 = _mk_base(admin_tok, DEFAULT_PROJECT, "bd3")
    t3 = _create_task(admin_tok, DEFAULT_PROJECT, a3["id"], "bnb4", f"500qbd3-{SUF}")["id"]
    out3 = _fake_out(t3)
    _finalize(t3, True)

    if _is_terminal(_task(admin_tok, t2)):
        pytest.skip("环境存在空闲 GPU，T2 已收敛终态，'进行中'分支无意义")

    r = requests.post(f"{BASE}/api/quantize/batch-delete", headers=H(admin_tok),
                      timeout=30, json={"ids": [t1, t2, t3, 999999]})
    r.raise_for_status()
    body = r.json()
    assert set(body) == {"deleted", "failed"}
    assert body["deleted"] == [t1], f"仅 T1 应可删: {body}"
    failed_ids = {f["id"] for f in body["failed"]}
    assert failed_ids == {t2, t3, 999999}, f"失败项不对: {body}"
    ferr = {f["id"]: f["error"] for f in body["failed"]}
    assert "进行中" in ferr[t2]
    assert "模型资产" in ferr[t3]
    assert "不存在" in ferr[999999]
    # 磁盘与 DB 状态
    assert not os.path.exists(_quant_task_dir(t1)), "已删任务目录未清理"
    assert requests.get(f"{BASE}/api/quantize/{t1}", headers=H(admin_tok),
                        timeout=15).status_code == 404
    assert requests.get(f"{BASE}/api/quantize/{t2}", headers=H(admin_tok),
                        timeout=15).status_code == 200, "进行中任务被误删"
    assert os.path.exists(out3), "受保护产物目录被误删"
    # 空列表
    r0 = requests.post(f"{BASE}/api/quantize/batch-delete", headers=H(admin_tok),
                       timeout=15, json={"ids": []})
    r0.raise_for_status()
    assert r0.json() == {"deleted": [], "failed": []}


def test_batch_delete_cross_tenant(qctx, admin_tok, tenant_b):
    """跨租户批量删除：逐项 403 语义（进入 failed 列表，不删任何任务）"""
    a = _mk_base(admin_tok, DEFAULT_PROJECT, "bdx")
    t = _create_task(admin_tok, DEFAULT_PROJECT, a["id"], "bnb4", f"500qbdt-{SUF}")["id"]
    _finalize(t, False, "exit code 1\nbatch cross-tenant case")
    _, _, dev_tok = tenant_b["dev"]
    r = requests.post(f"{BASE}/api/quantize/batch-delete", headers=H(dev_tok),
                      timeout=15, json={"ids": [t, 999999]})
    r.raise_for_status()
    body = r.json()
    assert body["deleted"] == [], f"跨租户不应删除任何任务: {body}"
    assert {f["id"] for f in body["failed"]} == {t, 999999}
    assert all("无权限" in f["error"] or "不存在" in f["error"] for f in body["failed"])
    assert requests.get(f"{BASE}/api/quantize/{t}", headers=H(admin_tok),
                        timeout=15).status_code == 200, "跨租户任务被误删"


# ================= E. 调度器状态机 =================

def test_reconcile_lost_context_failed(qctx, admin_tok):
    """E2E：任务 running 但 backend 进程内无 exec 上下文（模拟重启丢上下文），
    真调度器 tick 应收敛为 failed 并发 quant_failed 告警事件"""
    asset = _mk_base(admin_tok, DEFAULT_PROJECT, "lost")
    tid = _create_task(admin_tok, DEFAULT_PROJECT, asset["id"], "bnb4",
                       f"500qlost-{SUF}")["id"]
    db = _db()
    try:
        from app.models import QuantTask
        t = db.get(QuantTask, tid)
        t.status = "running"
        t.container_name = f"ft-quant-{tid}"
        db.commit()
    finally:
        db.close()

    def _wait():
        t = _task(admin_tok, tid)
        return t if _is_terminal(t) else None
    t = poll(_wait, timeout=90, interval=3, desc="调度器收敛 running→failed")
    assert t["status"] == "failed"
    assert "上下文丢失" in t["error"] or "调度器重启" in t["error"]
    # quant_failed 告警事件已入库
    db = _db()
    try:
        from app.models import AlertEvent
        n = db.query(AlertEvent).filter(
            AlertEvent.target == f"quant:{tid}").count()
        assert n >= 1, "失败任务未产生告警事件"
    finally:
        db.close()


def test_finalize_success_registers_artifact(qctx, admin_tok):
    """成功收敛：产物注册为 merged 资产（命名 quant-Nb-<源>、血缘 parent、项目一致）"""
    tid = qctx["task_bnb4"]
    out = _fake_out(tid)
    res = _finalize(tid, True)
    assert res["status"] == "succeeded", f"成功收敛应 succeeded: {res}"
    db = _db()
    try:
        from app.models import ModelAsset
        a = db.query(ModelAsset).filter(ModelAsset.path == out).first()
        assert a is not None, "量化产物未注册"
        assert a.type == "merged"
        assert a.project_id == DEFAULT_PROJECT
        assert a.parent_asset_id == qctx["asset_bnb4"]["id"], "血缘未指向源模型"
        assert a.name == f"quant-4b-{qctx['asset_bnb4']['name']}", f"命名不符: {a.name}"
        art_name = a.name
    finally:
        db.close()
    # 模型仓库 API 可见（前端资产列表来源）
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    assert any(x["name"] == art_name for x in rows), "注册产物未在模型仓库 API 中可见"


def test_finalize_success_empty_out_fails(qctx, admin_tok):
    """exit 0 但 out 目录为空 → 判 failed（未找到产物），不注册资产"""
    tid = qctx["task_bnb8"]
    # 确保无产物
    shutil.rmtree(os.path.join(_quant_task_dir(tid), "out"), ignore_errors=True)
    res = _finalize(tid, True)
    assert res["status"] == "failed"
    assert "未找到产物" in res["error"]


def test_finalize_failed_records_error_and_alert(qctx, admin_tok):
    """失败收敛：error 落库 + finished_at + quant_failed 告警事件（复用 awq4 任务，
    该任务在删除用例中因产物保护被拒删，仍存在于 DB）"""
    tid = qctx["task_awq4"]
    db = _db()
    try:
        from app.models import QuantTask, AlertEvent
        t = db.get(QuantTask, tid)
        assert t is not None
        before = db.query(AlertEvent).filter(
            AlertEvent.target == f"quant:{tid}").count()
    finally:
        db.close()
    res = _finalize(tid, False, "exit code 2\nAssertionError: fake")
    assert res["status"] == "failed"
    assert "exit code 2" in res["error"]
    assert res["finished_at"] is not None
    db = _db()
    try:
        from app.models import AlertEvent
        after = db.query(AlertEvent).filter(
            AlertEvent.target == f"quant:{tid}").count()
        assert after > before, "失败未产生 quant_failed 告警事件"
    finally:
        db.close()


# ================= F. 服务级（build_config / register_artifact） =================

def test_task_dir_under_data_dir():
    d = _quant_task_dir(123456)
    assert d.startswith(_data_dir().rstrip("/") + os.sep)
    assert d.endswith(os.path.join("quant", "123456"))


def test_build_config_all_methods():
    """4 种方式的 export 配置全字段（bit/method/device/export 目录）"""
    import yaml
    from app.models import QuantTask
    from app.services import quantize_service as qs
    expect = {
        "bnb4": (4, "bitsandbytes", "cpu"),
        "bnb8": (8, "bitsandbytes", "cpu"),
        "gptq4": (4, "gptq", "auto"),
        "awq4": (4, "awq", "auto"),
    }
    created = []
    try:
        for i, (m, (bit, method, device)) in enumerate(expect.items()):
            fake_id = 990000 + i
            t = QuantTask(id=fake_id, project_id=1, name=f"cfg-{m}",
                          model_asset_id=1, method=m, gpu_count=1,
                          status="pending", created_by=1)
            cfg = qs.build_config(t, f"/data/models/hub/fake-{m}")
            d = yaml.safe_load(open(cfg))
            assert d["model_name_or_path"] == f"/data/models/hub/fake-{m}"
            assert d["stage"] == "pt" and d["do_train"] is False
            assert d["quantization_bit"] == bit
            assert d["quantization_method"] == method
            assert d["export_device"] == device
            assert d["export_dir"] == os.path.join(qs.task_dir(fake_id), "out")
            assert d["export_size"] == 5
            assert d["trust_remote_code"] is True
            created.append(qs.task_dir(fake_id))
    finally:
        for d in created:
            shutil.rmtree(d, ignore_errors=True)


def test_register_artifact_name_dedup(qctx, admin_tok):
    """同项目重名自动 -N 递增；空产物目录返回 None"""
    from app.models import QuantTask, ModelAsset
    from app.services import quantize_service as qs
    tid = qctx["task_bnb4"]  # 已被 test_finalize_success_registers_artifact 注册过一次
    db = _db()
    try:
        t = db.get(QuantTask, tid)
        assert t is not None
        base_name = f"quant-4b-{qctx['asset_bnb4']['name']}"
        # 再次注册同名产物 → 应递增命名而非冲突
        a2 = qs.register_artifact(t)
        assert a2 is not None, "二次注册失败（out 目录应仍在）"
        assert a2.name.startswith(base_name) and a2.name != base_name, \
            f"重名未递增: {a2.name}（期望 {base_name}-N）"
        # 空产物目录 → None
        t3 = QuantTask(id=990099, project_id=t.project_id, name="empty-out",
                       model_asset_id=t.model_asset_id, method="bnb4",
                       gpu_count=1, status="succeeded", created_by=t.created_by)
        shutil.rmtree(qs.task_dir(990099), ignore_errors=True)
        assert qs.register_artifact(t3) is None
    finally:
        db.close()
        shutil.rmtree(qs.task_dir(990099), ignore_errors=True)

# -*- coding: utf-8 -*-
"""模型路由（部署自动关联 / 路由策略 / 路由级缓存）+ 缓存加速（类型/容量/逻辑型）集成测试。

所有实体带 SUF 后缀，自清理；不触碰常驻部署（DEPLOY_ID 仅作目标引用）。
"""
import time

import pytest
import requests
from conftest import (BASE, H, SUF, DEFAULT_PROJECT, DEPLOY_ID, DEPLOY_NAME,
                      BASE_MODEL_ASSET_ID)


def _free_local_gpus(admin_tok) -> int:
    """本机空闲 GPU 数（与 create_deploy 准入预检同口径 scheduler._gpu_in_use）"""
    r = requests.get(f"{BASE}/api/cluster/nodes", headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    local = next((n for n in r.json() if n.get("local")), None)
    if not local:
        return 0
    return max(0, (local.get("gpu_count") or 0) - (local.get("gpu_in_use") or 0))


# ---------- 缓存加速：类型/容量/逻辑型 ----------

def _mk_cache(name, **kw):
    r = requests.post(f"{BASE}/api/cache/services", headers=H(kw.pop("tok")), timeout=15,
                      json={"name": name, **kw})
    r.raise_for_status()
    return r.json()


def test_cache_type_capacity_crud(admin_tok):
    name = f"500cache-{SUF}"
    c = _mk_cache(name, tok=admin_tok, cache_type="ssd", capacity_gb=512,
                  description="集成测试", image="")
    assert c["cache_type"] == "ssd" and c["capacity_gb"] == 512
    assert c["status"] == "stopped" and not c["url"]
    # 非法类型 400
    r = requests.post(f"{BASE}/api/cache/services", headers=H(admin_tok), timeout=15,
                      json={"name": f"500cache-bad-{SUF}", "cache_type": "hdd", "image": ""})
    assert r.status_code == 400
    # 逻辑型（无镜像）不可启动
    r = requests.post(f"{BASE}/api/cache/services/{c['id']}/start", headers=H(admin_tok), timeout=15)
    assert r.status_code == 400
    # 编辑类型/容量（元数据，不重启）
    r = requests.put(f"{BASE}/api/cache/services/{c['id']}", headers=H(admin_tok), timeout=15,
                     json={"cache_type": "memory", "capacity_gb": 1024})
    r.raise_for_status()
    body = r.json()
    assert body["cache_type"] == "memory" and body["capacity_gb"] == 1024
    # 删除
    r = requests.delete(f"{BASE}/api/cache/services/{c['id']}", headers=H(admin_tok), timeout=15)
    r.raise_for_status()


# ---------- 路由：策略 / 路由级缓存 / 命名互斥解除 ----------

def test_route_policy_and_cache(admin_tok):
    name = f"500route-{SUF}"
    # 非法策略 400
    r = requests.post(f"{BASE}/api/inference/routes", headers=H(admin_tok), timeout=15,
                      json={"project_id": DEFAULT_PROJECT, "name": name, "policy": "bogus"})
    assert r.status_code == 400
    # 优先级策略：单目标归一化（权重 100 / 非容灾 / priority=1）
    r = requests.post(f"{BASE}/api/inference/routes", headers=H(admin_tok), timeout=15,
                      json={"project_id": DEFAULT_PROJECT, "name": name, "policy": "priority",
                            "targets": [{"deployment_id": DEPLOY_ID, "weight": 7}]})
    r.raise_for_status()
    rt = r.json()
    assert rt["policy"] == "priority" and rt["auto"] is False
    t0 = rt["targets"][0]
    assert t0["weight"] == 100 and t0["is_fallback"] is False and t0["priority"] == 1
    rid = rt["id"]
    # 移除常驻部署目标：后续缓存变更断言不得触碰常驻部署生命周期
    # （路由级缓存变更会重启运行中目标，曾因此引发生产事故）
    r = requests.delete(f"{BASE}/api/inference/routes/{rid}/targets/{t0['id']}",
                        headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    try:
        # 缓存服务引用（无目标 → 不触发目标重启）
        cname = f"500routecache-{SUF}"
        cs = _mk_cache(cname, tok=admin_tok, image="")
        r = requests.put(f"{BASE}/api/inference/routes/{rid}", headers=H(admin_tok), timeout=15,
                         json={"cache_service_id": cs["id"]})
        r.raise_for_status()
        assert r.json()["cache_service_id"] == cs["id"] and r.json()["cache_name"] == cname
        # 非法缓存 id 400
        r = requests.put(f"{BASE}/api/inference/routes/{rid}", headers=H(admin_tok), timeout=15,
                         json={"cache_service_id": 999999})
        assert r.status_code == 400
        # 被路由引用的缓存不可删 409
        r = requests.delete(f"{BASE}/api/cache/services/{cs['id']}", headers=H(admin_tok), timeout=15)
        assert r.status_code == 409
        # 清除路由缓存关联（显式 null 语义）
        r = requests.put(f"{BASE}/api/inference/routes/{rid}", headers=H(admin_tok), timeout=15,
                         json={"cache_service_id": None})
        r.raise_for_status()
        assert r.json()["cache_service_id"] is None
        # 缓存可删
        r = requests.delete(f"{BASE}/api/cache/services/{cs['id']}", headers=H(admin_tok), timeout=15)
        r.raise_for_status()
    finally:
        requests.delete(f"{BASE}/api/inference/routes/{rid}", headers=H(admin_tok), timeout=15)


def test_resident_deploy_has_same_name_auto_route(admin_tok):
    """启动回填：常驻部署自动生成同名 auto 路由（路由名与部署名互斥已解除的实证）。"""
    r = requests.get(f"{BASE}/api/inference/routes", headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    row = next((x for x in r.json() if x["name"] == DEPLOY_NAME), None)
    assert row, f"常驻部署 {DEPLOY_NAME} 缺少同名 auto 路由"
    assert row["auto"] is True
    assert any(t["deployment_id"] == DEPLOY_ID for t in row["targets"])


# ---------- 部署自动关联路由：创建 → 同名 auto 路由；删除 → GC ----------

def test_deploy_auto_route(admin_tok):
    dname = f"500auto-{SUF}"
    # GPU 准入：全量运行时前序测试文件的异步量化任务可能仍为 running（调度器
    # 收敛滞后一个 tick），占掉唯一空闲卡 → 等出空闲位，等不到则跳过
    for _ in range(40):  # 至多 ~120s
        if _free_local_gpus(admin_tok) >= 1:
            break
        time.sleep(3)
    else:
        pytest.skip("无空闲 GPU（前序测试异步任务未收敛），跳过自动关联断言")
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": dname,
                            "model_asset_id": BASE_MODEL_ASSET_ID,
                            "engine": "vllm", "tensor_parallel": 1, "replicas": 1,
                            "max_model_len": 2048})
    assert r.status_code == 200, r.text
    did = r.json()["id"]
    try:
        # 同名 auto 路由已自动创建（单目标=该部署）
        r = requests.get(f"{BASE}/api/inference/routes", headers=H(admin_tok), timeout=15)
        r.raise_for_status()
        row = next((x for x in r.json() if x["name"] == dname), None)
        assert row, f"部署 {dname} 未自动生成同名路由"
        assert row["auto"] is True and row["policy"] == "lb"
        assert len(row["targets"]) == 1 and row["targets"][0]["deployment_id"] == did
    finally:
        # 停止 + 删除（避免占卡），验证 auto 路由 GC
        requests.post(f"{BASE}/api/inference/deployments/{did}/stop",
                      headers=H(admin_tok), timeout=60)
        r = requests.delete(f"{BASE}/api/inference/deployments/{did}",
                            headers=H(admin_tok), timeout=60)
        r.raise_for_status()
    r = requests.get(f"{BASE}/api/inference/routes", headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    assert not any(x["name"] == dname for x in r.json()), "部署删除后 auto 路由未 GC"

# -*- coding: utf-8 -*-
"""镜像管理 API：组件清单 / 版本探测非阻塞回归（#e65aa49）/ 自定义镜像 CRUD

快速功能测试：
- 不触发 refresh（真实 pull/build 是后台长任务，属升级模块范围）
- 不触发 force probe（同步起容器探测可达数十秒）；仅验证列表探测走
  stale-while-revalidate 永不阻塞（backend 冷启动首开秒级返回）
- 自定义镜像仅 DB 登记（本地无该镜像 → status=missing），删除即清理
"""
import time

import requests

from conftest import BASE, H, SUF

COMP_KEYS = {"vllm", "sglang", "llamafactory", "gpu_agent"}
ITEM_FIELDS = {"key", "image", "component", "source", "exists", "size", "version", "desc"}


def test_list_components_shape(admin_tok):
    r = requests.get(f"{BASE}/api/images", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200
    rows = r.json()
    assert {x["key"] for x in rows} >= COMP_KEYS
    for x in rows:
        assert ITEM_FIELDS <= set(x), f"{x['key']} 缺字段: {ITEM_FIELDS - set(x)}"
        assert x["source"] in ("official", "selfbuilt", "custom")


def test_list_full_platform_images(admin_tok):
    """镜像管理页 = 平台全量镜像清单（19 项：5 引擎 + 3 自构 + 11 基础设施），
    与集群升级页同一数据源（upgrade_service.COMPONENTS）"""
    r = requests.get(f"{BASE}/api/images", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200
    rows = [x for x in r.json() if not x["key"].startswith("custom-")]
    assert len(rows) == 19, f"平台镜像清单应为 19 项，实际 {len(rows)}: {[x['key'] for x in rows]}"
    for key in ("dcgm_exporter", "node_exporter", "prometheus", "alertmanager",
                "grafana", "postgres", "redis", "opensearch", "argilla", "rustfs",
                "juicefs", "mineru", "backend", "web", "evalscope"):
        assert any(x["key"] == key for x in rows), f"清单缺组件 {key}"


def test_list_nonblocking_cold(admin_tok):
    """回归（e65aa49）：探测缓存过期/首探时列表接口立即返回（旧值或占位），
    不再同步起容器 import 引擎（曾导致页面首开 1-3 分钟空转）"""
    t0 = time.time()
    r = requests.get(f"{BASE}/api/images", headers=H(admin_tok), timeout=30)
    dt = time.time() - t0
    assert r.status_code == 200
    assert dt < 15, f"镜像列表应非阻塞快速返回，实际 {dt:.1f}s"


def test_probe_unknown_key_404(admin_tok):
    r = requests.post(f"{BASE}/api/images/no-such-key/probe",
                      headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_custom_crud_roundtrip(admin_tok):
    name = f"bdimg-{SUF}:v1"
    r = requests.post(f"{BASE}/api/images/custom", headers=H(admin_tok), timeout=15,
                      json={"name": name, "note": "crud test"})
    assert r.status_code == 200, r.text
    cid = r.json()["id"]
    # 本地无此镜像 → missing
    assert r.json()["status"] == "missing"
    rows = requests.get(f"{BASE}/api/images/custom", headers=H(admin_tok), timeout=15).json()
    assert any(x["id"] == cid and x["name"] == name for x in rows)
    # 汇总列表含自定义行（key=custom-{id}）
    allrows = requests.get(f"{BASE}/api/images", headers=H(admin_tok), timeout=30).json()
    assert any(x["key"] == f"custom-{cid}" and x["source"] == "custom" for x in allrows)
    r = requests.delete(f"{BASE}/api/images/custom/{cid}", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    rows = requests.get(f"{BASE}/api/images/custom", headers=H(admin_tok), timeout=15).json()
    assert all(x["id"] != cid for x in rows)


def test_custom_bad_name_400(admin_tok):
    r = requests.post(f"{BASE}/api/images/custom", headers=H(admin_tok), timeout=15,
                      json={"name": "no-colon-tag"})
    assert r.status_code == 400


def test_custom_duplicate_400(admin_tok):
    name = f"bdimg-dup-{SUF}:v1"
    r = requests.post(f"{BASE}/api/images/custom", headers=H(admin_tok), timeout=15,
                      json={"name": name})
    assert r.status_code == 200
    cid = r.json()["id"]
    try:
        r = requests.post(f"{BASE}/api/images/custom", headers=H(admin_tok), timeout=15,
                          json={"name": name})
        assert r.status_code == 400
    finally:
        requests.delete(f"{BASE}/api/images/custom/{cid}", headers=H(admin_tok), timeout=15)


def test_custom_delete_missing_404(admin_tok):
    r = requests.delete(f"{BASE}/api/images/custom/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_images_rbac_403(admin_tok, tenant_a):
    """镜像管理仅超管"""
    r = requests.get(f"{BASE}/api/images", headers=H(tenant_a["dev"][2]), timeout=15)
    assert r.status_code == 403
    r = requests.post(f"{BASE}/api/images/custom", headers=H(tenant_a["ta"][2]),
                      timeout=15, json={"name": "x/y:z"})
    assert r.status_code == 403

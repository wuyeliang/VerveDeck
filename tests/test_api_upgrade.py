# -*- coding: utf-8 -*-
"""集群升级 API：status / check / execute / tasks + RBAC + 校验分支

注意：本套件不触发真实升级（引擎升级会改写运行时 pin、agent/self 升级会重建容器，
对测试环境有破坏性副作用）——execute 仅覆盖建任务前的校验 400 分支。
"""
import requests

from conftest import BASE, H


def _status(admin_tok):
    r = requests.get(f"{BASE}/api/admin/upgrade/status", headers=H(admin_tok), timeout=60)
    r.raise_for_status()
    return r.json()


# ---------- 状态 ----------

# 平台全量镜像清单（与 upgrade_service.COMPONENTS 一致，19 项）
ALL_COMPONENT_KEYS = {
    "vllm", "sglang", "llamafactory", "gpu_agent", "evalscope", "mineru",
    "backend", "web", "dcgm_exporter", "node_exporter", "prometheus",
    "alertmanager", "grafana", "postgres", "redis", "opensearch", "argilla",
    "rustfs", "juicefs",
}


def test_upgrade_status_shape(admin_tok):
    rows = _status(admin_tok)
    assert {x["key"] for x in rows} == ALL_COMPONENT_KEYS
    assert len(rows) == 19
    for x in rows:
        assert {"key", "label", "image", "kind", "impact", "exists", "size",
                "runtime", "impact_hint", "running_task"} <= set(x)
        assert x["impact"] in ("engine", "agent", "self")
        assert x["kind"] in ("pull", "build")
        assert x["running_task"] in (0, None) or isinstance(x["running_task"], int)


def test_two_pages_image_list_identical(admin_tok):
    """回归：镜像管理页与集群升级页镜像清单完全一致——同一数据源
    （upgrade_service.COMPONENTS）同序渲染，(key, image) 序列逐项相等"""
    rows = _status(admin_tok)
    r = requests.get(f"{BASE}/api/images", headers=H(admin_tok), timeout=60)
    r.raise_for_status()
    imgs = [(x["key"], x["image"]) for x in r.json()
            if not x["key"].startswith("custom-")]
    assert [(x["key"], x["image"]) for x in rows] == imgs
    assert len(rows) == 19


def test_upgrade_status_selfbuilt_images_fintune(admin_tok):
    rows = {x["key"]: x for x in _status(admin_tok)}
    assert rows["backend"]["image"].startswith("fintune/backend:")
    assert rows["web"]["image"].startswith("fintune/web:")


# ---------- 版本检查（不触网分支） ----------

def test_upgrade_check_build_component_empty(admin_tok):
    r = requests.post(f"{BASE}/api/admin/upgrade/check", headers=H(admin_tok),
                      json={"target": "backend"}, timeout=30)
    r.raise_for_status()
    assert r.json() == {"target": "backend", "latest": []}


def test_upgrade_check_unknown_target_400(admin_tok):
    r = requests.post(f"{BASE}/api/admin/upgrade/check", headers=H(admin_tok),
                      json={"target": "nope"}, timeout=30)
    assert r.status_code == 400


def test_upgrade_check_missing_target_400(admin_tok):
    r = requests.post(f"{BASE}/api/admin/upgrade/check", headers=H(admin_tok),
                      json={}, timeout=30)
    assert r.status_code == 400


# ---------- 执行（仅校验分支，无副作用） ----------

def test_upgrade_execute_unknown_target_400(admin_tok):
    r = requests.post(f"{BASE}/api/admin/upgrade/execute", headers=H(admin_tok),
                      json={"target": "nope"}, timeout=30)
    assert r.status_code == 400


def test_upgrade_execute_pull_requires_version_400(admin_tok):
    r = requests.post(f"{BASE}/api/admin/upgrade/execute", headers=H(admin_tok),
                      json={"target": "vllm"}, timeout=30)
    assert r.status_code == 400


def test_upgrade_execute_bad_tag_format_400(admin_tok):
    r = requests.post(f"{BASE}/api/admin/upgrade/execute", headers=H(admin_tok),
                      json={"target": "vllm", "to_version": "latest"}, timeout=30)
    assert r.status_code == 400
    r = requests.post(f"{BASE}/api/admin/upgrade/execute", headers=H(admin_tok),
                      json={"target": "gpu_agent", "to_version": "4.7.1"}, timeout=30)
    assert r.status_code == 400  # 需 -ubuntu24.04 后缀


# ---------- 任务列表 ----------

def test_upgrade_tasks_shape(admin_tok):
    r = requests.get(f"{BASE}/api/admin/upgrade/tasks", headers=H(admin_tok), timeout=30)
    r.raise_for_status()
    items = r.json()
    assert isinstance(items, list)
    for t in items:
        assert {"id", "target", "from_version", "to_version", "status", "log",
                "created_at", "finished_at"} <= set(t)
        assert t["status"] in ("running", "success", "failed")
        assert t["target"] in ALL_COMPONENT_KEYS


def test_upgrade_tasks_limit(admin_tok):
    r = requests.get(f"{BASE}/api/admin/upgrade/tasks", headers=H(admin_tok),
                     params={"limit": 1}, timeout=30)
    r.raise_for_status()
    assert len(r.json()) <= 1


# ---------- RBAC ----------

def test_upgrade_rbac_forbidden(roles):
    for tok in (roles["tenant_admin"], roles["developer"], roles["viewer"]):
        r = requests.get(f"{BASE}/api/admin/upgrade/status", headers=H(tok), timeout=15)
        assert r.status_code == 403
        r = requests.post(f"{BASE}/api/admin/upgrade/execute", headers=H(tok),
                          json={"target": "backend"}, timeout=15)
        assert r.status_code == 403
        r = requests.get(f"{BASE}/api/admin/upgrade/tasks", headers=H(tok), timeout=15)
        assert r.status_code == 403


def test_upgrade_unauthenticated():
    r = requests.get(f"{BASE}/api/admin/upgrade/status", timeout=15)
    assert r.status_code in (401, 403)

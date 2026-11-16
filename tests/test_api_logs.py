# -*- coding: utf-8 -*-
"""日志中心 API：可查服务列表 / 容器日志读取 / RBAC

快速功能测试：只读操作，无副作用。
- 平台容器 fintune-{service}-1 由 compose 保证存在（backend 自身必在其中）
- 任务容器 ft-* 视环境可能有 0..N 个（只断言 kind 字段语义，不依赖数量）
- 非 compose 环境（无 fintune-backend-1 容器）整组 skip
"""
import pytest
import requests

from conftest import BASE, H

PLATFORM = {"backend", "web", "gpu-agent", "postgres", "redis", "grafana"}


def _platform_present(admin_tok) -> bool:
    try:
        r = requests.get(f"{BASE}/api/logs/services", headers=H(admin_tok), timeout=30)
        rows = r.json()
        be = next((x for x in rows if x["name"] == "backend"), None)
        return bool(be and be["running"])
    except Exception:
        return False


@pytest.fixture(scope="module")
def guard(admin_tok):
    if not _platform_present(admin_tok):
        pytest.skip("无 fintune-backend-1 容器（非 compose 环境）")
    return admin_tok


def test_services_list_shape(guard):
    r = requests.get(f"{BASE}/api/logs/services", headers=H(guard), timeout=30)
    assert r.status_code == 200
    rows = r.json()
    assert {"backend", "web"} <= {x["name"] for x in rows}
    for x in rows:
        assert {"name", "container", "running", "kind"} <= set(x)
        assert x["kind"] in ("platform", "task")
    # 平台容器名遵循 fintune-{service}-1
    be = next(x for x in rows if x["name"] == "backend")
    assert be["container"] == "fintune-backend-1" and be["running"] is True


def test_services_task_prefix(guard):
    """任务容器一律 ft-* 前缀（ft-train-N / ft-infer-N / ft-eval-N ...）"""
    rows = requests.get(f"{BASE}/api/logs/services", headers=H(guard), timeout=30).json()
    tasks = [x for x in rows if x["kind"] == "task"]
    assert all(x["name"].startswith("ft-") for x in tasks)


def test_read_backend_logs(guard):
    r = requests.get(f"{BASE}/api/logs", params={"service": "backend", "tail": 50},
                     headers=H(guard), timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert body["service"] == "backend" and body["container"] == "fintune-backend-1"
    assert body["running"] is True
    assert isinstance(body["lines"], list) and body["lines"], "backend 日志应非空"


def test_read_logs_tail_clamped(guard):
    """tail 下限 10 上限 5000：0 → 按 10 返回"""
    r = requests.get(f"{BASE}/api/logs", params={"service": "backend", "tail": 0},
                     headers=H(guard), timeout=30)
    assert r.status_code == 200
    assert len(r.json()["lines"]) >= 1


def test_read_logs_missing_404(guard):
    r = requests.get(f"{BASE}/api/logs",
                     params={"service": "no-such-svc-xyz"}, headers=H(guard), timeout=30)
    assert r.status_code == 404


def test_logs_rbac_403(guard, tenant_a):
    """日志仅超管（含任务容器日志，敏感）"""
    r = requests.get(f"{BASE}/api/logs/services", headers=H(tenant_a["ta"][2]), timeout=15)
    assert r.status_code == 403
    r = requests.get(f"{BASE}/api/logs", params={"service": "backend"},
                     headers=H(tenant_a["dev"][2]), timeout=15)
    assert r.status_code == 403

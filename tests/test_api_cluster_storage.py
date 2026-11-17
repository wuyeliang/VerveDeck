# -*- coding: utf-8 -*-
"""存储 IO 监控（读/写吞吐 + IO 延时/利用率）+ 默认存储告警规则

端点 /api/cluster/storage，数据底座 node-exporter 磁盘指标。
磁盘空闲窗口 io_ms 可能为 None（0/0=NaN 按"无数据"处理）——属正常行为，
且 NaN/inf 不得出现在响应中（starlette allow_nan=False 会 500）。
"""
import math

import requests

from conftest import BASE, H


def test_storage_structure(admin_tok):
    r = requests.get(f"{BASE}/api/cluster/storage", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200, r.text
    d = r.json()
    assert set(d) >= {"node", "latest", "series"}
    assert isinstance(d["series"], list)
    if d["latest"] is not None:
        assert set(d["latest"]) >= {"ts", "read_bps", "write_bps", "io_ms", "io_util_pct"}
        for k in ("read_bps", "write_bps", "io_ms", "io_util_pct"):
            v = d["latest"][k]
            assert v is None or isinstance(v, (int, float)), f"{k}={v!r} 应为数值或 None"


def test_storage_values_finite(admin_tok):
    """序列内不得出现 NaN/inf；每行必须含 ts"""
    r = requests.get(f"{BASE}/api/cluster/storage?minutes=30", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200, r.text
    for row in r.json()["series"]:
        assert isinstance(row, dict) and "ts" in row
        for k, v in row.items():
            if k == "ts":
                continue
            assert v is None or math.isfinite(v), f"series {k}={v!r} 非有限值"


def test_storage_minutes_param(admin_tok):
    r = requests.get(f"{BASE}/api/cluster/storage?minutes=10", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["series"], list)


def test_storage_unauth_401():
    r = requests.get(f"{BASE}/api/cluster/storage", timeout=30)
    assert r.status_code == 401


def test_storage_default_alert_rules(admin_tok):
    """存储 IO 两条默认告警规则必须存在（存量库由 init_db 幂等补建）"""
    r = requests.get(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200, r.text
    by_metric = {x["metric"]: x for x in r.json() if x["kind"] == "threshold"}
    assert "host_disk_io_ms" in by_metric, "缺默认规则：磁盘 IO 延时过高"
    assert "host_disk_util" in by_metric, "缺默认规则：磁盘 IO 利用率过高"
    assert by_metric["host_disk_io_ms"]["op"] == ">"
    assert by_metric["host_disk_io_ms"]["threshold"] == 200
    assert by_metric["host_disk_util"]["op"] == ">"
    assert by_metric["host_disk_util"]["threshold"] == 90


def test_storage_rule_metric_accepted(admin_tok):
    """新建规则时新指标键可通过 METRIC_NAMES 校验（不再 400）"""
    r = requests.post(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=30, json={
        "name": "stg-read-probe", "kind": "threshold", "metric": "host_disk_read",
        "op": ">", "threshold": 1, "severity": "warning", "cooldown": 300, "enabled": False})
    assert r.status_code == 200, r.text
    rid = r.json()["id"]
    rb = requests.delete(f"{BASE}/api/admin/alerts/rules/{rid}", headers=H(admin_tok), timeout=30)
    assert rb.status_code == 200, rb.text

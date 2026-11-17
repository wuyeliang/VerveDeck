# -*- coding: utf-8 -*-
"""告警扩展（2026-11-17）：规则分类 + 2000 容量（分页/批量导入）+ 事件确认/删除/批量操作。

规则列表接口升级为 {total, items} 分页结构；事件接口新增 acked 字段与筛选。
事件行的 ack/删除验证需要真实事件行：测试经 backend 进程库直写
（套件运行于 backend 容器内，SessionLocal 与后端同库），再走 HTTP 验证。
"""
import sys
import os
import time

import pytest
import requests

sys.path.insert(0, "/app")
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "platform", "backend")))

from conftest import BASE, H, SUF  # noqa: E402


# ---------- 规则：分类 ----------

def _mk_rule(admin_tok, name, category="", kind="threshold", metric="gpu_util",
             op=">", thr=91, event="", sev="warning", cd=300, enabled=False):
    body = {"name": name, "kind": kind, "metric": metric, "op": op, "threshold": thr,
            "event": event, "severity": sev, "cooldown": cd, "enabled": enabled,
            "category": category}
    r = requests.post(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=30, json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_rule_category_crud(admin_tok):
    cat = f"cat-{SUF}"
    rid = _mk_rule(admin_tok, f"rcat-{SUF}", category=cat)
    # 列表按分类筛选
    r = requests.get(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=15,
                     params={"category": cat, "limit": 100})
    items = r.json()["items"]
    assert any(x["id"] == rid and x["category"] == cat for x in items)
    assert all(x["category"] == cat for x in items), "分类筛选应精确命中"
    # 分类去重列表
    r = requests.get(f"{BASE}/api/admin/alerts/rules/categories", headers=H(admin_tok), timeout=15)
    assert cat in r.json()["categories"]
    # 更新分类
    r = requests.put(f"{BASE}/api/admin/alerts/rules/{rid}", headers=H(admin_tok), timeout=30,
                     json={"name": f"rcat-{SUF}", "kind": "threshold", "metric": "gpu_util",
                           "op": ">", "threshold": 91, "severity": "warning",
                           "cooldown": 300, "enabled": False, "category": ""})
    assert r.status_code == 200, r.text
    r = requests.get(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=15,
                     params={"q": f"rcat-{SUF}"})
    row = next(x for x in r.json()["items"] if x["id"] == rid)
    assert row["category"] == ""
    # 超长分类拒绝
    r = requests.post(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=30,
                      json={"name": f"rlong-{SUF}", "kind": "threshold", "metric": "gpu_util",
                            "op": ">", "threshold": 1, "severity": "warning",
                            "enabled": False, "category": "x" * 65})
    assert r.status_code == 400
    # 清理
    requests.delete(f"{BASE}/api/admin/alerts/rules/{rid}", headers=H(admin_tok), timeout=30)


def test_rule_name_search_and_kind_filter(admin_tok):
    _mk_rule(admin_tok, f"rq-{SUF}-t", kind="threshold", metric="host_cpu", thr=88)
    rid2 = _mk_rule(admin_tok, f"rq-{SUF}-e", kind="event", event="train_failed")
    r = requests.get(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=15,
                     params={"q": f"rq-{SUF}", "kind": "event"})
    items = r.json()["items"]
    assert [x["id"] for x in items] == [rid2], "q+kind 组合筛选"
    requests.delete(f"{BASE}/api/admin/alerts/rules/{rid2}", headers=H(admin_tok), timeout=30)


# ---------- 规则：分页（2000 容量支撑） ----------

def test_rule_pagination(admin_tok):
    rids = [_mk_rule(admin_tok, f"rp-{SUF}-{i}", metric="gpu_mem", thr=80 + i) for i in range(3)]
    try:
        r = requests.get(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=15,
                         params={"q": f"rp-{SUF}", "limit": 2, "offset": 0})
        body = r.json()
        assert body["total"] == 3
        assert len(body["items"]) == 2
        r = requests.get(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=15,
                         params={"q": f"rp-{SUF}", "limit": 2, "offset": 2})
        body2 = r.json()
        assert body2["total"] == 3 and len(body2["items"]) == 1
        assert body2["items"][0]["id"] not in {x["id"] for x in body["items"]}
        # limit 上限 1000（传 5000 不报错）
        r = requests.get(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=30,
                         params={"limit": 5000})
        assert r.status_code == 200 and "items" in r.json()
    finally:
        for rid in rids:
            requests.delete(f"{BASE}/api/admin/alerts/rules/{rid}", headers=H(admin_tok), timeout=30)


def test_rule_quota_at_least_2000():
    """容量要求：规则上限 ≥2000（实现取 5000 留余量）"""
    from app.api.admin import MAX_ALERT_RULES
    assert MAX_ALERT_RULES >= 2000, f"规则上限 {MAX_ALERT_RULES} 低于需求 2000"


# ---------- 规则：批量创建（导入） ----------

def test_rule_batch_create(admin_tok):
    ok_rules = [{"name": f"rb-{SUF}-{i}", "kind": "threshold", "metric": "gpu_temp",
                 "op": ">", "threshold": 60 + i, "severity": "warning", "cooldown": 300,
                 "enabled": False, "category": f"bc-{SUF}"} for i in range(3)]
    bad_rules = [
        {"name": f"rb-bad-{SUF}", "kind": "threshold", "metric": "no_such_metric",
         "op": ">", "threshold": 1, "severity": "warning", "enabled": False},
        {"name": f"rb-bad2-{SUF}", "kind": "event", "event": "no_such_event",
         "severity": "warning", "enabled": False},
    ]
    r = requests.post(f"{BASE}/api/admin/alerts/rules/batch", headers=H(admin_tok), timeout=60,
                      json={"rules": ok_rules + bad_rules})
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["created"]) == 3, body
    assert len(body["failed"]) == 2, body
    assert {f["index"] for f in body["failed"]} == {3, 4}
    # 空数组 / 超限拒绝
    r = requests.post(f"{BASE}/api/admin/alerts/rules/batch", headers=H(admin_tok), timeout=30,
                      json={"rules": []})
    assert r.status_code == 400
    r = requests.post(f"{BASE}/api/admin/alerts/rules/batch", headers=H(admin_tok), timeout=30,
                      json={"rules": [{"name": "x", "kind": "threshold", "metric": "gpu_util",
                                        "op": ">", "threshold": 1, "enabled": False}] * 1001})
    assert r.status_code == 400
    # 清理
    for rid in body["created"]:
        requests.delete(f"{BASE}/api/admin/alerts/rules/{rid}", headers=H(admin_tok), timeout=30)


# ---------- 事件：确认 / 取消确认 / 删除 / 批量 ----------

def _seed_events(n: int, target: str) -> list[int]:
    """直写事件行（测试进程与 backend 同库）"""
    from app.database import SessionLocal
    from app.models import AlertEvent
    db = SessionLocal()
    try:
        ids = []
        for i in range(n):
            e = AlertEvent(ts=int(time.time()), rule_id=None, level="warning",
                           target=f"{target}:{i}", title=f"ext-test-{SUF}-{i}",
                           detail="")
            db.add(e)
            db.flush()
            ids.append(e.id)
        db.commit()
        return ids
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def test_event_ack_unack_delete(admin_tok):
    ids = _seed_events(3, f"evt-{SUF}")
    try:
        # 未确认状态（默认）
        r = requests.get(f"{BASE}/api/admin/alerts/events", headers=H(admin_tok), timeout=15,
                         params={"target": f"evt-{SUF}", "limit": 50})
        items = r.json()["items"]
        assert len(items) == 3
        assert all(e["acked"] is False and e["acked_by"] == "" and e["acked_at"] == "" for e in items)
        # 批量确认 2 条
        r = requests.post(f"{BASE}/api/admin/alerts/events/ack", headers=H(admin_tok), timeout=30,
                          json={"ids": ids[:2], "ack": True})
        assert r.status_code == 200, r.text
        assert sorted(r.json()["acked"]) == sorted(ids[:2])
        r = requests.get(f"{BASE}/api/admin/alerts/events", headers=H(admin_tok), timeout=15,
                         params={"target": f"evt-{SUF}", "acked": "1", "limit": 50})
        items = r.json()["items"]
        assert sorted(e["id"] for e in items) == sorted(ids[:2]), "acked=1 仅返回已确认"
        assert all(e["acked"] is True and e["acked_by"] == "admin" for e in items)
        assert all(e["acked_at"] for e in items)
        # 幂等：重复确认不重复计
        r = requests.post(f"{BASE}/api/admin/alerts/events/ack", headers=H(admin_tok), timeout=30,
                          json={"ids": [ids[0]], "ack": True})
        assert r.json()["acked"] == []
        # 取消确认 1 条
        r = requests.post(f"{BASE}/api/admin/alerts/events/ack", headers=H(admin_tok), timeout=30,
                          json={"ids": [ids[1]], "ack": False})
        assert sorted(r.json()["unacked"]) == [ids[1]]
        r = requests.get(f"{BASE}/api/admin/alerts/events", headers=H(admin_tok), timeout=15,
                         params={"target": f"evt-{SUF}", "acked": "0", "limit": 50})
        assert sorted(e["id"] for e in r.json()["items"]) == sorted([ids[1], ids[2]])
        # 批量删除 2 条
        r = requests.post(f"{BASE}/api/admin/alerts/events/batch-delete", headers=H(admin_tok),
                          timeout=30, json={"ids": [ids[1], ids[2]]})
        assert r.status_code == 200, r.text
        assert sorted(r.json()["deleted"]) == sorted([ids[1], ids[2]])
        r = requests.get(f"{BASE}/api/admin/alerts/events", headers=H(admin_tok), timeout=15,
                         params={"target": f"evt-{SUF}", "limit": 50})
        assert [e["id"] for e in r.json()["items"]] == [ids[0]]
        # 参数校验
        r = requests.post(f"{BASE}/api/admin/alerts/events/ack", headers=H(admin_tok), timeout=30,
                          json={"ids": [], "ack": True})
        assert r.status_code == 400
        r = requests.post(f"{BASE}/api/admin/alerts/events/batch-delete", headers=H(admin_tok),
                          timeout=30, json={"ids": [1] * 1001})
        assert r.status_code == 400
    finally:
        # 兜底清理（ids[0] 若已被删除则 no-op）
        requests.post(f"{BASE}/api/admin/alerts/events/batch-delete", headers=H(admin_tok),
                      timeout=30, json={"ids": [ids[0]]})


def test_event_ack_requires_auth():
    """无 token 不可确认/删除事件（RBAC 矩阵由 test_api_rbac 覆盖）"""
    r = requests.post(f"{BASE}/api/admin/alerts/events/ack",
                      json={"ids": [1], "ack": True}, timeout=15)
    assert r.status_code in (401, 403), f"无 token 应 401/403，实际 {r.status_code}"
    r = requests.post(f"{BASE}/api/admin/alerts/events/batch-delete",
                      json={"ids": [1]}, timeout=15)
    assert r.status_code in (401, 403)

# -*- coding: utf-8 -*-
"""prom_service 单测：规则/Alertmanager YAML 渲染 + am_webhook 回调 + 节点心跳 tick

DB 用内存 sqlite 隔离；不触真实 Prometheus / 网络（必要时 monkeypatch）。
"""
import time

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models as M
from app.config import settings
from app.services import prom_service as ps
from app.services import alert_service as asvc
from app.services import gpu_service as gs


@pytest.fixture()
def sdb():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    yield s
    s.close()


class _Rule:
    """AlertRule 最小替身（render_rules_yaml 只读属性）"""
    def __init__(self, id, name, metric, op=">", thr=85, sev="warning"):
        self.id, self.name, self.metric = id, name, metric
        self.op, self.threshold, self.severity = op, thr, sev


# ---------------- 规则渲染 ----------------

def test_render_rules_yaml_basic():
    rules = [_Rule(1, "GPU 温度过高", "gpu_temp", ">", 85, "critical"),
             _Rule(2, "GPU XID 错误", "gpu_xid", ">", 0, "critical"),
             _Rule(3, "推理服务离线", "infer_down", "<", 1, "critical"),
             _Rule(4, "推理 P95 延迟", "infer_latency_p95", ">", 2.5, "warning")]
    doc = yaml.safe_load(ps.render_rules_yaml(rules))
    rs = doc["groups"][0]["rules"]
    assert [r["alert"] for r in rs] == ["GpuTempHigh85", "GpuXidError0", "InferDown1",
                                        "InferLatencyP95High2.5"]
    assert rs[0]["expr"] == "DCGM_FI_DEV_GPU_TEMP > 85"
    assert rs[0]["for"] == "60s"
    assert rs[0]["labels"] == {"severity": "critical", "source": "vervedeck",
                               "metric": "gpu_temp", "category": "default"}
    # description 保留 Prometheus 模板变量
    assert "{{ $value }}" in rs[0]["annotations"]["description"]
    assert rs[2]["expr"] == 'up{job="inference"} < 1'
    assert rs[3]["expr"].endswith("> 2.5")


def test_render_rules_yaml_dedup_and_skip_unknown():
    rules = [_Rule(1, "A", "kv_cache", ">", 90, "warning"),
             _Rule(2, "B", "kv_cache", ">", 90, "warning"),
             _Rule(3, "坏指标", "no_such_metric", ">", 1, "warning")]
    doc = yaml.safe_load(ps.render_rules_yaml(rules))
    rs = doc["groups"][0]["rules"]
    assert [r["alert"] for r in rs] == ["KvCacheHigh90", "KvCacheHigh90R2"]


def test_render_rules_yaml_empty():
    doc = yaml.safe_load(ps.render_rules_yaml([]))
    assert doc["groups"] == []  # 空规则 → 空 groups（Prometheus 合法，无告警评估）


def test_render_rules_yaml_category_groups():
    """分类拆 group：ASCII 分类用 slug、中文分类用短 hash、未分类保持旧组名（2000+ 规则性能）"""
    rules = [_Rule(1, "A", "gpu_temp", ">", 85, "critical"),
             _Rule(2, "B", "gpu_temp", ">", 90, "warning"),
             _Rule(3, "C", "host_cpu", ">", 90, "warning"),
             _Rule(4, "D", "kv_cache", ">", 90, "warning")]
    # 给替身补分类（render 经 getattr 读，缺省 ""）
    rules[0].category = "GPU"
    rules[1].category = "GPU"
    rules[2].category = "系统"
    # rules[3] 无分类
    doc = yaml.safe_load(ps.render_rules_yaml(rules))
    by_name = {g["name"]: g["rules"] for g in doc["groups"]}
    assert len(doc["groups"]) == 3
    assert len(by_name["vervedeck-alerts-gpu"]) == 2  # 同分类同组
    assert by_name["vervedeck-alerts-gpu"][0]["labels"]["category"] == "GPU"
    assert "vervedeck-alerts" in by_name  # 未分类保持旧组名
    assert by_name["vervedeck-alerts"][0]["labels"]["category"] == "default"
    # 中文分类 → hash 片段（稳定唯一，非 ASCII）
    zh_group = [n for n in by_name if n.startswith("vervedeck-alerts-c")]
    assert len(zh_group) == 1 and by_name[zh_group[0]][0]["labels"]["category"] == "系统"
    # alertname 跨组仍唯一
    all_names = [r["alert"] for g in doc["groups"] for r in g["rules"]]
    assert len(all_names) == len(set(all_names)) == 4


# ---------------- Alertmanager 渲染 ----------------

def _am_cfg(**over):
    cfg = {
        "wecom_enabled": True,
        "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=x",
        "smtp_enabled": True, "smtp_host": "smtp.exmail.qq.com", "smtp_port": 465,
        "smtp_user": "a@x.com", "smtp_password": "pw", "smtp_from": "a@x.com",
        "smtp_to": "b@x.com,c@d.com",
    }
    cfg.update(over)
    return cfg


def test_render_am_yaml_email_wecom(monkeypatch):
    monkeypatch.setattr(settings, "backend_internal_url", "http://backend:3001")
    monkeypatch.setattr(settings, "am_webhook_token", "tk-test")
    doc = yaml.safe_load(ps.render_am_yaml(_am_cfg()))
    rcv = doc["receivers"][0]
    assert rcv["name"] == "default"
    assert rcv["email_configs"][0]["to"] == "b@x.com,c@d.com"
    assert rcv["email_configs"][0]["send_resolved"] is True
    assert rcv["webhook_configs"][0]["url"] == "http://backend:3001/api/internal/am-webhook"
    assert rcv["webhook_configs"][0]["http_headers"] == {"X-Vervedeck-Token": "tk-test"}
    g = doc["global"]
    assert g["smtp_host"] == "smtp.exmail.qq.com" and g["smtp_require_tls"] is False
    assert doc["route"]["group_by"] == ["alertname", "instance"]
    assert doc["route"]["routes"][0]["matchers"] == ['severity="critical"']


def test_render_am_yaml_wecom_only():
    cfg = _am_cfg(smtp_enabled=False, smtp_host="", smtp_user="", smtp_to="")
    doc = yaml.safe_load(ps.render_am_yaml(cfg))
    rcv = doc["receivers"][0]
    assert "email_configs" not in rcv and "webhook_configs" in rcv
    assert "smtp_host" not in doc["global"]


def test_render_am_yaml_null_receiver():
    doc = yaml.safe_load(ps.render_am_yaml({"wecom_enabled": False, "smtp_enabled": False}))
    assert doc["route"]["receiver"] == "null"
    assert doc["receivers"] == [{"name": "null"}]  # AM 不允许字面 null，用同名空 receiver


# ---------------- am_webhook 回调 ----------------

def _am_payload(status="firing", alertname="GpuTempHigh85", instance="gpu:0"):
    return {"status": status, "alerts": [{
        "labels": {"alertname": alertname, "severity": "critical",
                   "instance": instance, "job": "dcgm-exporter"},
        "annotations": {"summary": "GPU 温度过高：GPU 温度 > 85",
                        "description": "GPU 0 温度 92°C"},
    }]}


def test_am_webhook_firing_dedup(sdb, monkeypatch):
    monkeypatch.setattr(asvc, "channel_config",
                        lambda: {"wecom_enabled": False, "wecom_webhook": ""})
    out = ps.am_webhook(sdb, _am_payload())
    assert out["ok"] is True and out["fired"] == 1 and out["total"] == 1
    evs = sdb.query(M.AlertEvent).all()
    assert len(evs) == 1
    assert evs[0].level == "critical" and evs[0].target == "gpu:0"
    assert evs[0].title.startswith("GpuTempHigh85")
    # 300s 内同 alertname+目标去重
    assert ps.am_webhook(sdb, _am_payload())["fired"] == 0
    assert sdb.query(M.AlertEvent).count() == 1
    # 不同目标不去重
    assert ps.am_webhook(sdb, _am_payload(instance="gpu:1"))["fired"] == 1


def test_am_webhook_resolved_info(sdb, monkeypatch):
    monkeypatch.setattr(asvc, "channel_config",
                        lambda: {"wecom_enabled": False, "wecom_webhook": ""})
    ps.am_webhook(sdb, _am_payload("firing"))
    out = ps.am_webhook(sdb, _am_payload("resolved"))
    assert out["status"] == "resolved"
    evs = sdb.query(M.AlertEvent).order_by(M.AlertEvent.id.desc()).all()
    assert len(evs) == 2
    assert evs[0].level == "info" and "已恢复" in evs[0].title


def test_am_webhook_wecom_thread(sdb, monkeypatch):
    """wecom 启用时 firing 会走 _send_wecom 线程（打桩验证调用）"""
    calls = []
    monkeypatch.setattr(asvc, "channel_config",
                        lambda: {"wecom_enabled": True,
                                 "wecom_webhook": "https://qyapi.weixin.qq.com/x"})
    monkeypatch.setattr(asvc, "_send_wecom",
                        lambda title, body, cfg: calls.append(title))
    import app.services.prom_service as _ps

    class _FakeThread:
        def __init__(self, target=None, args=(), daemon=None):
            self.target, self.args = target, args

        def start(self):
            # 同步执行打桩的 _send_wecom（生产代码调用 .start()）
            if self.target:
                self.target(*self.args)
    monkeypatch.setattr(_ps, "threading",
                        type("T", (), {"Thread": _FakeThread})())
    ps.am_webhook(sdb, _am_payload())
    time.sleep(0.2)
    assert len(calls) == 1 and calls[0].startswith("GpuTempHigh85")


# ---------------- GPU 快照：心跳 reconcile（#75 回归：GPU 设备页偶现 2 卡） ----------------

class _StubNode:
    """gpu_snapshot 只读 node.name / node.meta 的最小替身"""
    def __init__(self, names=None, name=None):
        self.name = name or settings.node_name
        self.meta = {"names": names} if names else {}


def _prom_by_index(indexes, ts=1_000_000.0, val="42"):
    """prom_instant 替身：每个索引一条序列（模拟 DCGM 多索引/多代际可见）"""
    def _instant(expr):
        return [{"metric": {"gpu": str(i)}, "value": [ts + i, val]} for i in indexes]
    return _instant


def test_gpu_snapshot_heartbeat_list_drops_stale_index(monkeypatch):
    """心跳仅 1 卡时，DCGM 里越界的陈旧索引（旧 exporter 代际/重枚举残留）不渲染成卡"""
    monkeypatch.setattr(ps, "prom_instant", _prom_by_index([0, 1]))
    snap = ps.gpu_snapshot(_StubNode(names=["NVIDIA GeForce RTX 3080"]))
    assert [g["index"] for g in snap] == [0]
    assert snap[0]["name"] == "NVIDIA GeForce RTX 3080"
    assert snap[0]["util"] == 42


def test_gpu_snapshot_freshest_wins_for_dup_index(monkeypatch):
    """同索引多序列同时可见（容器代际切换窗口）：取时间戳最新样本，口径确定"""
    def _instant(expr):
        return [
            {"metric": {"gpu": "0"}, "value": [1000.0, "11"]},
            {"metric": {"gpu": "0"}, "value": [2000.0, "33"]},
        ]
    monkeypatch.setattr(ps, "prom_instant", _instant)
    snap = ps.gpu_snapshot(_StubNode(names=["GPU A"]))
    assert len(snap) == 1 and snap[0]["util"] == 33


def test_gpu_snapshot_remote_node_renders_no_local_dcgm(monkeypatch):
    """注册/残留的其他节点行不渲染本集群 DCGM（防同一物理卡以多节点行重复出卡）"""
    monkeypatch.setattr(ps, "prom_instant", _prom_by_index([0]))
    snap = ps.gpu_snapshot(_StubNode(names=None, name="remote-node"))
    assert snap == []


def test_gpu_snapshot_local_node_fallback_without_names(monkeypatch):
    """本节点心跳列表缺失（agent 未就绪窗口）：回退按 DCGM 序列推导"""
    monkeypatch.setattr(ps, "prom_instant", _prom_by_index([0, 1]))
    snap = ps.gpu_snapshot(_StubNode(names=None))
    assert [g["index"] for g in snap] == [0, 1]


def test_gpu_snapshot_names_without_dcgm_zero_rows(monkeypatch):
    """exporter 未就绪：按心跳登记列表给空行，卡数仍等于宿主真实卡数"""
    monkeypatch.setattr(ps, "prom_instant", lambda expr: [])
    snap = ps.gpu_snapshot(_StubNode(names=["GPU A", "GPU B"]))
    assert [g["index"] for g in snap] == [0, 1]
    assert all(g["util"] == 0 for g in snap)


# ---------------- 节点心跳 tick ----------------

def test_gpu_tick_online_and_offline(sdb, monkeypatch, tmp_path):
    monkeypatch.setattr(gs, "SessionLocal", lambda: sdb)
    monkeypatch.setattr(gs, "HEARTBEAT_FILE", str(tmp_path / "agent.ts"))
    monkeypatch.setattr(gs, "NAMES_FILE", str(tmp_path / "agent.names"))
    monkeypatch.setattr(gs, "_fail_count", 0)
    events = []
    monkeypatch.setattr(asvc, "trigger_event",
                        lambda db, code, target, title, detail="": events.append(code))
    (tmp_path / "agent.names").write_text("0,NVIDIA GeForce RTX 4090\n")
    (tmp_path / "agent.ts").write_text(str(int(time.time())))

    sdb.add(M.GpuNode(name=settings.node_name, gpu_count=0, status="offline"))
    sdb.commit()
    gs.tick()
    node = sdb.query(M.GpuNode).filter_by(name=settings.node_name).first()
    assert node.status == "online" and node.gpu_count == 1
    assert node.meta["names"] == ["NVIDIA GeForce RTX 4090"]
    assert events == ["node_online"]

    # 心跳过期：连续 2 次 tick 才判离线（写入瞬间防抖）
    (tmp_path / "agent.ts").write_text(str(int(time.time()) - 120))
    gs.tick()
    assert sdb.query(M.GpuNode).filter_by(name=settings.node_name).first().status == "online"
    gs.tick()
    assert sdb.query(M.GpuNode).filter_by(name=settings.node_name).first().status == "offline"
    assert events[-1] == "node_offline"


def test_gpu_tick_no_heartbeat_file(sdb, monkeypatch, tmp_path):
    monkeypatch.setattr(gs, "SessionLocal", lambda: sdb)
    monkeypatch.setattr(gs, "HEARTBEAT_FILE", str(tmp_path / "missing.ts"))
    monkeypatch.setattr(gs, "NAMES_FILE", str(tmp_path / "missing.names"))
    monkeypatch.setattr(gs, "_fail_count", 5)  # 视为已连续多 tick 无心跳
    monkeypatch.setattr(asvc, "trigger_event", lambda *a, **k: None)
    sdb.add(M.GpuNode(name=settings.node_name, gpu_count=1, status="online"))
    sdb.commit()
    gs.tick()
    assert sdb.query(M.GpuNode).filter_by(name=settings.node_name).first().status == "offline"


def test_gpu_tick_gpu_list_follows_heartbeat_idempotent(sdb, monkeypatch, tmp_path):
    """心跳 reconcile：gpu_count/meta.names 每次心跳按当前 GPU 列表对账，幂等不重复发事件"""
    monkeypatch.setattr(gs, "SessionLocal", lambda: sdb)
    monkeypatch.setattr(gs, "HEARTBEAT_FILE", str(tmp_path / "agent.ts"))
    monkeypatch.setattr(gs, "NAMES_FILE", str(tmp_path / "agent.names"))
    monkeypatch.setattr(gs, "_fail_count", 0)
    events = []
    monkeypatch.setattr(asvc, "trigger_event",
                        lambda db, code, target, title, detail="": events.append(code))
    (tmp_path / "agent.names").write_text("0,NVIDIA GeForce RTX 3080\n")
    (tmp_path / "agent.ts").write_text(str(int(time.time())))

    gs.tick()  # 节点不存在 → 自动登记
    node = sdb.query(M.GpuNode).filter_by(name=settings.node_name).first()
    assert node.status == "online" and node.gpu_count == 1
    assert node.meta["names"] == ["NVIDIA GeForce RTX 3080"]
    assert events == []  # 新登记节点本即 online，不触发恢复事件

    # GPU 数量变化（加卡）：以心跳当前列表为准
    (tmp_path / "agent.names").write_text(
        "0,NVIDIA GeForce RTX 3080\n1,NVIDIA GeForce RTX 3080\n")
    gs.tick()
    node = sdb.query(M.GpuNode).filter_by(name=settings.node_name).first()
    assert node.gpu_count == 2 and len(node.meta["names"]) == 2

    # 幂等：列表不变时重复 tick 状态不变、不重复发事件
    gs.tick()
    node = sdb.query(M.GpuNode).filter_by(name=settings.node_name).first()
    assert node.gpu_count == 2 and node.status == "online"
    assert events == []
    assert sdb.query(M.GpuNode).filter_by(name=settings.node_name).count() == 1

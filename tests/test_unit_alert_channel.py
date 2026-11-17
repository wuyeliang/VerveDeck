# -*- coding: utf-8 -*-
"""通知通道配置单测：DB 优先 env 兜底 / 密码掩码 / 保存保持密码 / 按通道测试

网络与 SMTP 全部 mock；_cfg_row 打桩避免触真实 DB。
"""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models as M
from app.config import settings
from app.services import alert_service as asvc


@pytest.fixture()
def sdb():
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    yield s
    s.close()


@pytest.fixture()
def env_cfg(monkeypatch):
    """env 基线：wecom 已配、smtp 已配"""
    monkeypatch.setattr(settings, "wecom_webhook", "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=env")
    monkeypatch.setattr(settings, "smtp_host", "smtp.env.com")
    monkeypatch.setattr(settings, "smtp_port", 465)
    monkeypatch.setattr(settings, "smtp_user", "env@x.com")
    monkeypatch.setattr(settings, "smtp_password", "env-pw")
    monkeypatch.setattr(settings, "smtp_from", "env@x.com")
    monkeypatch.setattr(settings, "alert_email_to", "ops@x.com")
    monkeypatch.setattr(asvc, "_cfg_row", lambda: None)


# ---------------- 生效配置解析 ----------------

def test_channel_config_env_only(env_cfg):
    cfg = asvc.channel_config()
    assert cfg["wecom_webhook"].endswith("key=env") and cfg["wecom_enabled"]
    assert cfg["smtp_host"] == "smtp.env.com" and cfg["smtp_ssl"] is True
    assert cfg["smtp_to"] == "ops@x.com"


def test_channel_config_db_override(sdb, env_cfg, monkeypatch):
    row = M.AlertChannelConfig(id=1, wecom_enabled=True,
                               wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=db")
    sdb.add(row)
    sdb.commit()
    monkeypatch.setattr(asvc, "_cfg_row", lambda: sdb.get(M.AlertChannelConfig, 1))
    cfg = asvc.channel_config()
    # wecom 被 DB 覆盖；smtp DB 未配置 → 回退 env
    assert cfg["wecom_webhook"].endswith("key=db")
    assert cfg["smtp_host"] == "smtp.env.com"


def test_channel_config_db_smtp_fallback_fields(env_cfg, monkeypatch):
    """DB 只配 smtp_user 也视为 smtp 通道已启用（整体覆盖，字段取 DB）"""
    row = M.AlertChannelConfig(id=1, smtp_enabled=True, smtp_user="db@x.com",
                               smtp_password="db-pw", smtp_to="a@x.com,b@x.com")
    monkeypatch.setattr(asvc, "_cfg_row", lambda: row)
    cfg = asvc.channel_config()
    assert cfg["smtp_user"] == "db@x.com"
    assert cfg["smtp_password"] == "db-pw"
    assert cfg["smtp_to"] == "a@x.com,b@x.com"
    assert cfg["smtp_host"] == ""  # 整体覆盖：DB 未填 host


# ---------------- 配置读写（密码掩码/保持） ----------------

def test_get_config_masks_password(sdb, env_cfg, monkeypatch):
    row = M.AlertChannelConfig(id=1, wecom_enabled=True,
                               wecom_webhook="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=db",
                               smtp_enabled=True, smtp_host="smtp.db.com", smtp_port=587,
                               smtp_ssl=False, smtp_user="db@x.com", smtp_password="s3cret",
                               smtp_from="db@x.com", smtp_to="ops@x.com")
    sdb.add(row)
    sdb.commit()
    monkeypatch.setattr(asvc, "_cfg_row", lambda: sdb.get(M.AlertChannelConfig, 1))
    out = asvc.get_config(sdb)
    assert "s3cret" not in str(out)
    assert out["smtp_password_set"] is True
    assert out["wecom_source"] == "db" and out["smtp_source"] == "db"
    assert out["smtp_port"] == 587 and out["smtp_ssl"] is False


def test_get_config_no_row_sources(sdb, env_cfg):
    out = asvc.get_config(sdb)
    assert out["wecom_source"] == "env" and out["smtp_source"] == "env"


def test_save_config_creates_and_keeps_password(sdb, env_cfg, monkeypatch):
    out = asvc.save_config(sdb, {
        "wecom_enabled": True, "wecom_webhook": "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=1",
        "smtp_enabled": True, "smtp_host": "smtp.db.com", "smtp_port": 465, "smtp_ssl": True,
        "smtp_user": "db@x.com", "smtp_password": "first-pw", "smtp_from": "", "smtp_to": "a@x.com",
    })
    row = sdb.get(M.AlertChannelConfig, 1)
    assert row.smtp_password == "first-pw"
    assert out["smtp_source"] == "db"
    # 掩码回传 = 保持原密码
    asvc.save_config(sdb, {"smtp_password": asvc.PW_MASK})
    sdb.expire_all()
    assert sdb.get(M.AlertChannelConfig, 1).smtp_password == "first-pw"
    # 明文回传 = 更新密码
    asvc.save_config(sdb, {"smtp_password": "second-pw"})
    sdb.expire_all()
    assert sdb.get(M.AlertChannelConfig, 1).smtp_password == "second-pw"


# ---------------- 通道选择 ----------------

def _rule(channels):
    class R:
        pass
    r = R()
    r.channels = channels
    return r


def test_channels_selection(env_cfg):
    cfg = asvc.channel_config()
    assert asvc._channels(_rule([]), cfg) == ["wecom", "email"]
    assert asvc._channels(_rule(["email"]), cfg) == ["email"]
    # 规则只指定 wecom，但 DB 未配 → 只剩 wecom
    monkey_cfg = dict(cfg, wecom_webhook="https://x", smtp_enabled=False)
    assert asvc._channels(_rule(["wecom", "email"]), monkey_cfg) == ["wecom"]


def test_notify_thread_uses_snap_not_orm(env_cfg, monkeypatch):
    """回归：异步通知线程用标量快照而非 ORM 实例（DetachedInstanceError 修复）

    生产路径 fire() → commit（属性过期）→ 请求结束后会话关闭 → 后台线程
    直接访问 rule/ev ORM 实例会抛 DetachedInstanceError（123 环境
    登录锁定告警曾复现）。此处：会话仍开时取快照并内联执行线程目标，
    随后关闭会话，用同一快照再跑一遍 _do_notify——旧实现在此处必然抛
    DetachedInstanceError（读 ev.level/rule.channels），新实现只碰标量。
    """
    import time as _time
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    class _FakeThread:
        def __init__(self, target=None, args=(), **kw):
            self._target, self._args = target, args

        def start(self):
            self._target(*self._args)

        def join(self, timeout=None):
            pass

    eng = create_engine("sqlite://", poolclass=StaticPool,
                        connect_args={"check_same_thread": False})
    M.AlertEvent.__table__.create(eng)
    S = sessionmaker(bind=eng)
    monkeypatch.setattr(asvc, "SessionLocal", S)
    monkeypatch.setattr(asvc.threading, "Thread", _FakeThread)
    calls = []
    monkeypatch.setattr(asvc, "_send_one", lambda ch, t, b, c: calls.append(ch))

    s = S()
    ev = M.AlertEvent(ts=int(_time.time()), rule_id=None, level="info",
                      target="snap", title="[测试] 快照通知", detail="detail-x")
    s.add(ev)
    s.commit()
    s.refresh(ev)
    ev_snap = asvc._ev_snap(ev)  # 生产时序：会话未关时取快照
    asvc._notify(_rule(["wecom", "email"]), ev)  # 线程目标内联执行
    s.expire_all()
    s.close()  # 模拟请求结束、会话关闭
    # 关闭后再用快照跑一遍：旧实现在此抛 DetachedInstanceError
    # （SQLAlchemy 2.0 中 detached 实例连 PK 读取也抛错——即生产线程崩溃点）
    assert asvc._do_notify(asvc._RuleSnap([]), ev_snap) is True
    s2 = S()
    try:
        assert s2.get(M.AlertEvent, ev_snap[0]).notified is True
    finally:
        s2.close()
    assert calls == ["wecom", "email", "wecom", "email"]


# ---------------- 按通道测试 ----------------

def test_test_channel_unknown(sdb):
    r = asvc.test_channel(sdb, "sms")
    assert r["ok"] is False


def test_test_channel_unconfigured(sdb, monkeypatch):
    monkeypatch.setattr(settings, "wecom_webhook", "")
    monkeypatch.setattr(settings, "smtp_host", "")
    monkeypatch.setattr(settings, "smtp_user", "")
    monkeypatch.setattr(settings, "alert_email_to", "")
    monkeypatch.setattr(asvc, "_cfg_row", lambda: None)
    r = asvc.test_channel(sdb, "wecom")
    assert r["ok"] is False and "未启用" in r["error"]
    r = asvc.test_channel(sdb, "email")
    assert r["ok"] is False


class _FakeResp:
    status_code = 200
    text = '{"errcode":0}'

    def json(self):
        return {"errcode": 0}


def test_test_channel_wecom_success(sdb, env_cfg, monkeypatch):
    monkeypatch.setattr(asvc, "_cfg_row", lambda: None)
    monkeypatch.setattr(asvc.requests, "post", lambda *a, **k: _FakeResp())
    r = asvc.test_channel(sdb, "wecom")
    assert r["ok"] is True
    evs = sdb.query(M.AlertEvent).all()
    assert len(evs) == 1 and evs[0].notified is True and evs[0].rule_id is None


def test_test_channel_wecom_error(sdb, env_cfg, monkeypatch):
    monkeypatch.setattr(asvc, "_cfg_row", lambda: None)

    def bad_post(*a, **k):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(asvc.requests, "post", bad_post)
    r = asvc.test_channel(sdb, "wecom")
    assert r["ok"] is False and "connection refused" in r["error"]
    evs = sdb.query(M.AlertEvent).all()
    assert evs[0].notified is False


def test_test_channel_email_success(sdb, env_cfg, monkeypatch):
    monkeypatch.setattr(asvc, "_cfg_row", lambda: None)

    class _FakeSMTP:
        def __init__(self, *a, **k):
            pass

        def login(self, u, p):
            assert u == "env@x.com" and p == "env-pw"

        def sendmail(self, frm, to, msg):
            assert to == ["ops@x.com"]

        def quit(self):
            pass

    monkeypatch.setattr(asvc.smtplib, "SMTP_SSL", _FakeSMTP)
    r = asvc.test_channel(sdb, "email")
    assert r["ok"] is True

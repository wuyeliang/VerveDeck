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


# ---------------- evaluate() GPU 指标键映射回归 ----------------

def test_evaluate_gpu_rules_key_mapping(sdb, monkeypatch):
    """回归：规则指标名 gpu_temp/gpu_mem/gpu_util 须映射到 _gpu_rows 列键（曾 KeyError 'gpu_temp'）"""
    fired = []
    monkeypatch.setattr(asvc, "fire",
                        lambda db, rule, target, title, detail="": fired.append((rule.metric, target)))
    for metric in ("gpu_temp", "gpu_mem", "gpu_util"):
        sdb.add(M.AlertRule(name=f"r-{metric}", kind="threshold", metric=metric,
                            op=">", threshold=85, severity="warning", cooldown=0))
    sdb.commit()
    monkeypatch.setattr(asvc, "_gpu_rows", lambda db: [
        {"gpu": 0, "util": 99, "temp": 90, "mem_pct": 97},   # 三指标均越限
        {"gpu": 1, "util": 10, "temp": 45, "mem_pct": 20},   # 均正常
    ])
    asvc.evaluate(sdb)
    got = set(fired)
    assert ("gpu_temp", "gpu:0") in got
    assert ("gpu_mem", "gpu:0") in got
    assert ("gpu_util", "gpu:0") in got
    assert ("gpu_temp", "gpu:1") not in got
    assert ("gpu_mem", "gpu:1") not in got
    assert ("gpu_util", "gpu:1") not in got

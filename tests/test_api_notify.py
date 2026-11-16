# -*- coding: utf-8 -*-
"""通知通道 API：配置读写 / 校验 400 / 按通道测试 / RBAC

副作用控制：PUT 写入后恢复空配置（空配置 = 回退 env，不影响测试环境原有行为）；
仅当两通道均 source=none 时才调用 test 端点（避免真实外发）。
"""
import requests

from conftest import BASE, H

WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-500-suite"


def _cfg(admin_tok):
    r = requests.get(f"{BASE}/api/admin/notify-config", headers=H(admin_tok), timeout=30)
    r.raise_for_status()
    return r.json()


def test_notify_config_shape(admin_tok):
    cfg = _cfg(admin_tok)
    assert {"wecom_enabled", "wecom_webhook", "smtp_enabled", "smtp_host", "smtp_port",
            "smtp_ssl", "smtp_user", "smtp_password_set", "smtp_from", "smtp_to",
            "wecom_source", "smtp_source", "updated_at"} <= set(cfg)
    assert "smtp_password" not in cfg  # 明文密码不外泄
    assert cfg["wecom_source"] in ("db", "env", "none")
    assert cfg["smtp_source"] in ("db", "env", "none")


def test_notify_config_port_400(admin_tok):
    r = requests.put(f"{BASE}/api/admin/notify-config", headers=H(admin_tok),
                     json={"smtp_port": 1234}, timeout=30)
    assert r.status_code == 400


def test_notify_config_bad_webhook_400(admin_tok):
    r = requests.put(f"{BASE}/api/admin/notify-config", headers=H(admin_tok),
                     json={"wecom_enabled": True, "wecom_webhook": "http://evil.example/x"},
                     timeout=30)
    assert r.status_code == 400


def test_notify_config_put_get_roundtrip(admin_tok):
    """写入 → 读回（source=db）→ 恢复空配置（回退 env）"""
    r = requests.put(f"{BASE}/api/admin/notify-config", headers=H(admin_tok),
                     json={"wecom_enabled": True, "wecom_webhook": WEBHOOK,
                           "smtp_enabled": False}, timeout=30)
    r.raise_for_status()
    body = r.json()
    assert body["wecom_source"] == "db"
    assert body["wecom_webhook"] == WEBHOOK
    cfg = _cfg(admin_tok)
    assert cfg["wecom_enabled"] is True
    assert cfg["wecom_webhook"] == WEBHOOK
    # 恢复空配置（空 = 该通道回退 env，测试环境原行为不受影响）
    r = requests.put(f"{BASE}/api/admin/notify-config", headers=H(admin_tok),
                     json={"wecom_enabled": False, "wecom_webhook": "",
                           "smtp_enabled": False, "smtp_host": "", "smtp_port": 465,
                           "smtp_ssl": True, "smtp_user": "", "smtp_password": "",
                           "smtp_from": "", "smtp_to": ""}, timeout=30)
    r.raise_for_status()


def test_notify_config_keep_password_mask(admin_tok):
    """smtp_password 回传掩码 = 保持原值（PUT 为整单替换语义，与前端全表单提交一致）"""
    full = {"smtp_enabled": False, "smtp_host": "smtp.test.local",
            "smtp_port": 465, "smtp_ssl": True, "smtp_user": "t@x.com",
            "smtp_password": "s3cret-test-pw", "smtp_from": "", "smtp_to": "",
            "wecom_enabled": False, "wecom_webhook": ""}
    r = requests.put(f"{BASE}/api/admin/notify-config", headers=H(admin_tok),
                     json=full, timeout=30)
    r.raise_for_status()
    assert r.json()["smtp_password_set"] is True
    # 整单回传掩码（模拟用户只改其他字段后保存）：原密码保持
    full["smtp_password"] = "******"
    r = requests.put(f"{BASE}/api/admin/notify-config", headers=H(admin_tok),
                     json=full, timeout=30)
    r.raise_for_status()
    assert r.json()["smtp_password_set"] is True
    assert r.json()["smtp_host"] == "smtp.test.local"  # 其余字段未被破坏
    # 清理
    requests.put(f"{BASE}/api/admin/notify-config", headers=H(admin_tok),
                 json={"smtp_enabled": False, "smtp_host": "", "smtp_port": 465,
                       "smtp_ssl": True, "smtp_user": "", "smtp_password": "",
                       "smtp_from": "", "smtp_to": ""}, timeout=30).raise_for_status()


def test_notify_test_unconfigured(admin_tok):
    """两通道均未配置时 test 端点返回 ok=false（无外发）；已配置则跳过（防真实外发）"""
    cfg = _cfg(admin_tok)
    if cfg["wecom_source"] != "none" or cfg["smtp_source"] != "none":
        return  # 环境已配置通道：不做真实外发测试
    for ch in ("wecom", "email"):
        r = requests.post(f"{BASE}/api/admin/notify-config/test", headers=H(admin_tok),
                          json={"channel": ch}, timeout=30)
        r.raise_for_status()
        body = r.json()
        assert body["ok"] is False and body["channel"] == ch
    r = requests.post(f"{BASE}/api/admin/notify-config/test", headers=H(admin_tok),
                      json={"channel": "sms"}, timeout=30)
    r.raise_for_status()
    assert r.json()["ok"] is False


def test_notify_rbac_forbidden(roles):
    for tok in (roles["tenant_admin"], roles["developer"], roles["viewer"]):
        r = requests.get(f"{BASE}/api/admin/notify-config", headers=H(tok), timeout=15)
        assert r.status_code == 403
        r = requests.put(f"{BASE}/api/admin/notify-config", headers=H(tok),
                         json={}, timeout=15)
        assert r.status_code == 403
        r = requests.post(f"{BASE}/api/admin/notify-config/test", headers=H(tok),
                          json={"channel": "wecom"}, timeout=15)
        assert r.status_code == 403

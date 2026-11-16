# -*- coding: utf-8 -*-
"""TC-AUTH 认证与账户 API（login / me / 改密 / 禁用）"""
import pytest
import requests

from conftest import BASE, H, SUF, login


# ---------------- login ----------------

def test_login_admin_success(admin_tok):
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": "admin", "password": "admin123"}, timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body["token"] and body["user"]["username"] == "admin"
    assert body["user"]["role"] == "super_admin"


def test_login_wrong_password_401():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": "admin", "password": "nope"}, timeout=15)
    assert r.status_code == 401


def test_login_unknown_user_401():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": f"ghost-{SUF}", "password": "x"}, timeout=15)
    assert r.status_code == 401


def test_login_empty_password_401():
    r = requests.post(f"{BASE}/api/auth/login",
                      json={"username": "admin", "password": ""}, timeout=15)
    assert r.status_code == 401


def test_login_missing_fields_422():
    r = requests.post(f"{BASE}/api/auth/login", json={"username": "admin"}, timeout=15)
    assert r.status_code == 422
    r = requests.post(f"{BASE}/api/auth/login", json={"password": "x"}, timeout=15)
    assert r.status_code == 422
    r = requests.post(f"{BASE}/api/auth/login", json={}, timeout=15)
    assert r.status_code == 422


@pytest.fixture(scope="module")
def acct(admin_tok):
    """独立测试账户（改密/禁用场景，避免污染 admin）"""
    uname = f"acct500-{SUF}"
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": uname, "password": "pass12345",
                            "role": "developer"})
    r.raise_for_status()
    return {"username": uname, "uid": r.json()["id"], "password": "pass12345"}


def test_me_with_token(acct):
    tok = login(acct["username"], acct["password"])
    r = requests.get(f"{BASE}/api/auth/me", headers=H(tok), timeout=15)
    assert r.status_code == 200
    m = r.json()
    assert m["username"] == acct["username"]
    assert m["role"] == "developer"
    assert "id" in m and "tenant_id" in m


def test_me_without_token_401():
    assert requests.get(f"{BASE}/api/auth/me", timeout=15).status_code == 401


def test_me_garbage_token_401():
    assert requests.get(f"{BASE}/api/auth/me",
                        headers=H("garbage.token.here"), timeout=15).status_code == 401


def test_me_tampered_token_401(acct):
    tok = login(acct["username"], acct["password"])
    tampered = tok[:-4] + ("aaaa" if not tok.endswith("aaaa") else "bbbb")
    assert requests.get(f"{BASE}/api/auth/me",
                        headers=H(tampered), timeout=15).status_code == 401


def test_me_wrong_scheme_401(acct):
    tok = login(acct["username"], acct["password"])
    r = requests.get(f"{BASE}/api/auth/me",
                     headers={"Authorization": f"Basic {tok}"}, timeout=15)
    assert r.status_code in (401, 403)


# ---------------- 改密 ----------------

def test_change_password_wrong_old_400(acct):
    tok = login(acct["username"], acct["password"])
    r = requests.put(f"{BASE}/api/auth/password", headers=H(tok), timeout=15,
                     json={"old_password": "wrong-old", "new_password": "newpass99"})
    assert r.status_code == 400
    # 原密码仍可用
    assert login(acct["username"], acct["password"])


def test_change_password_roundtrip(acct):
    tok = login(acct["username"], acct["password"])
    new_pw = f"new500-{SUF}"
    r = requests.put(f"{BASE}/api/auth/password", headers=H(tok), timeout=15,
                     json={"old_password": acct["password"], "new_password": new_pw})
    assert r.status_code == 200 and r.json()["ok"] is True
    assert requests.post(f"{BASE}/api/auth/login",
                         json={"username": acct["username"], "password": acct["password"]},
                         timeout=15).status_code == 401
    tok2 = login(acct["username"], new_pw)
    assert requests.get(f"{BASE}/api/auth/me", headers=H(tok2), timeout=15).status_code == 200
    acct["password"] = new_pw  # 供后续用例使用


def test_change_password_missing_fields_422(acct):
    tok = login(acct["username"], acct["password"])
    r = requests.put(f"{BASE}/api/auth/password", headers=H(tok), timeout=15, json={})
    assert r.status_code == 422


# ---------------- 禁用账户 ----------------

# ---------------- 登录防暴力破解（锁定 + 告警） ----------------

def _login_cfg(admin_tok) -> dict:
    r = requests.get(f"{BASE}/api/admin/login-config", headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    return {k: r.json().get(k) for k in
            ("ip_whitelist_enabled", "ip_whitelist", "user_attempts",
             "ip_attempts", "lock_minutes")}


def _set_login_cfg(admin_tok, **over):
    body = _login_cfg(admin_tok)
    body.update(over)
    r = requests.put(f"{BASE}/api/admin/login-config", headers=H(admin_tok), timeout=15,
                     json=body)
    r.raise_for_status()


def test_lockout_after_failures_fires_alert(admin_tok):
    """连续失败达阈值 → 423 锁定 + login_locked 告警事件入库；解锁前正确密码也被拒。
    阈值临时调小（账号 3 次），finally 恢复原配置，不影响其他用例。"""
    uname = f"ghost500-{SUF}"
    prev = _login_cfg(admin_tok)
    try:
        _set_login_cfg(admin_tok, user_attempts=3, ip_attempts=1000, lock_minutes=1)
        for i in range(2):
            r = requests.post(f"{BASE}/api/auth/login",
                              json={"username": uname, "password": f"bad-{i}"}, timeout=15)
            assert r.status_code == 401, f"锁定前失败应 401（第 {i+1} 次），实际 {r.status_code}"
        r = requests.post(f"{BASE}/api/auth/login",
                          json={"username": uname, "password": "bad-2"}, timeout=15)
        assert r.status_code == 423, f"达阈值应 423 锁定，实际 {r.status_code}: {r.text[:200]}"
        # 锁定窗口内即使（假想的）正确密码也被拒
        r = requests.post(f"{BASE}/api/auth/login",
                          json={"username": uname, "password": "whatever"}, timeout=15)
        assert r.status_code == 423, f"锁定窗口内应持续 423，实际 {r.status_code}"
        # 锁定触发 → login_locked 告警事件入库（target=login:<ip>，可经 系统管理→告警 查看）
        r = requests.get(f"{BASE}/api/admin/alerts/events", headers=H(admin_tok),
                         params={"target": "login:", "limit": 50}, timeout=15)
        r.raise_for_status()
        items = r.json()["items"]
        assert any("暴力破解" in e.get("title", "") for e in items), \
            f"未找到 login_locked 告警事件: {[e.get('title') for e in items][:5]}"
    finally:
        _set_login_cfg(admin_tok, **prev)


def test_login_config_requires_admin():
    """登录安全配置为超管专属"""
    r = requests.get(f"{BASE}/api/admin/login-config", timeout=15)
    assert r.status_code == 401


def test_create_user_weak_password_400(admin_tok):
    """密码策略：创建用户 <8 位拒绝，≥8 位通过"""
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": f"weak500-{SUF}", "password": "short12",
                            "role": "viewer"})
    assert r.status_code == 400, f"7 位密码应 400，实际 {r.status_code}"
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": f"weak500-{SUF}", "password": "pass12345",
                            "role": "viewer"})
    assert r.status_code == 200, f"9 位密码应通过，实际 {r.status_code}: {r.text[:200]}"


def test_change_password_short_400(acct):
    """改密 <8 位拒绝（原密码不受影响）"""
    tok = login(acct["username"], acct["password"])
    r = requests.put(f"{BASE}/api/auth/password", headers=H(tok), timeout=15,
                     json={"old_password": acct["password"], "new_password": "short1"})
    assert r.status_code == 400, f"6 位新密码应 400，实际 {r.status_code}"
    assert login(acct["username"], acct["password"])  # 原密码仍有效


def test_audit_records_real_client_ip(admin_tok):
    """审计日志记真实客户端 IP（X-Real-IP，nginx 反代下为 $remote_addr）：
    未授权 401 属安全事件必留痕，ip 字段应为请求携带的 X-Real-IP。"""
    fake_ip = f"10.66.{int(SUF[-2:], 16) % 250 + 1}.77"  # 每次运行唯一的合法内网 IP
    r = requests.get(f"{BASE}/api/auth/me",
                     headers={"X-Real-IP": fake_ip}, timeout=15)
    assert r.status_code == 401
    r = requests.get(f"{BASE}/api/admin/audit", headers=H(admin_tok),
                     params={"action": "security", "limit": 20}, timeout=15)
    r.raise_for_status()
    items = r.json()["items"]
    assert any(i.get("ip") == fake_ip for i in items), \
        f"审计未记录 X-Real-IP（{fake_ip}）: {[i.get('ip') for i in items][:5]}"


def test_disabled_user_cannot_login(acct, admin_tok):
    r = requests.put(f"{BASE}/api/admin/users/{acct['uid']}", headers=H(admin_tok),
                     timeout=15, json={"is_active": False})
    assert r.status_code == 200
    try:
        r = requests.post(f"{BASE}/api/auth/login",
                          json={"username": acct["username"], "password": acct["password"]},
                          timeout=15)
        assert r.status_code == 403, f"禁用账户应 403，实际 {r.status_code}"
    finally:
        requests.put(f"{BASE}/api/admin/users/{acct['uid']}", headers=H(admin_tok),
                     timeout=15, json={"is_active": True})
    # 重新启用后可登录
    assert login(acct["username"], acct["password"])

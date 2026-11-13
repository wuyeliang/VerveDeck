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

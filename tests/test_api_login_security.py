# -*- coding: utf-8 -*-
"""登录安全配置 API（#76）：IP 白名单 + 防暴破锁定参数

安全约束（测试自身不能把环境锁死）：
- 只改数值参数与白名单文本；ip_whitelist_enabled 始终保持原值（测试环境
  若启用了白名单，套件自身可能依赖放行条目——绝不代为开启/变更开关）
- module fixture teardown 无条件恢复原始配置（供后续文件/下一轮套件使用）
"""
import pytest
import requests

from conftest import BASE, H

FIELDS = {"ip_whitelist_enabled", "ip_whitelist", "user_attempts",
          "ip_attempts", "lock_minutes", "source", "updated_at"}


def _get(admin_tok):
    r = requests.get(f"{BASE}/api/admin/login-config", headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    return r.json()


def _put(admin_tok, **over):
    base = _get(admin_tok)
    body = {"ip_whitelist_enabled": base["ip_whitelist_enabled"],  # 开关保持原值
            "ip_whitelist": base["ip_whitelist"],
            "user_attempts": base["user_attempts"],
            "ip_attempts": base["ip_attempts"],
            "lock_minutes": base["lock_minutes"]}
    body.update(over)
    return requests.put(f"{BASE}/api/admin/login-config", headers=H(admin_tok),
                        timeout=15, json=body)


@pytest.fixture(scope="module")
def lcfg(admin_tok):
    """原始配置快照；teardown 恢复"""
    orig = _get(admin_tok)
    yield orig
    try:
        _put(admin_tok, ip_whitelist=orig["ip_whitelist"],
             user_attempts=orig["user_attempts"], ip_attempts=orig["ip_attempts"],
             lock_minutes=orig["lock_minutes"])
    except Exception:
        pass


def test_get_shape(lcfg):
    assert FIELDS <= set(lcfg)
    assert lcfg["source"] in ("default", "db")
    assert lcfg["user_attempts"] >= 1 and lcfg["lock_minutes"] >= 1


def test_put_roundtrip_numeric(admin_tok, lcfg):
    r = _put(admin_tok, user_attempts=7, ip_attempts=40, lock_minutes=20)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user_attempts"] == 7 and body["ip_attempts"] == 40
    assert body["lock_minutes"] == 20
    assert body["source"] == "db"
    # 落库确认
    assert _get(admin_tok)["user_attempts"] == 7


def test_put_whitelist_normalized(admin_tok, lcfg):
    """白名单文本归一化：换行/空格/逗号混合 → 逗号分隔"""
    r = _put(admin_tok, ip_whitelist="10.0.0.0/8, 192.168.1.5\n172.16.0.0/12,")
    assert r.status_code == 200, r.text
    assert r.json()["ip_whitelist"] == "10.0.0.0/8,192.168.1.5,172.16.0.0/12"


def test_put_whitelist_bad_entry_400(admin_tok, lcfg):
    assert _put(admin_tok, ip_whitelist="not-an-ip").status_code == 400
    assert _put(admin_tok, ip_whitelist="10.0.0.0/33").status_code == 400


def test_put_attempts_out_of_range_400(admin_tok, lcfg):
    assert _put(admin_tok, user_attempts=0).status_code == 400
    assert _put(admin_tok, user_attempts=101).status_code == 400
    assert _put(admin_tok, ip_attempts=0).status_code == 400
    assert _put(admin_tok, ip_attempts=1001).status_code == 400
    assert _put(admin_tok, lock_minutes=0).status_code == 400
    assert _put(admin_tok, lock_minutes=1441).status_code == 400


def test_put_boundary_values_ok(admin_tok, lcfg):
    r = _put(admin_tok, user_attempts=1, ip_attempts=1, lock_minutes=1)
    assert r.status_code == 200
    r = _put(admin_tok, user_attempts=100, ip_attempts=1000, lock_minutes=1440)
    assert r.status_code == 200


def test_get_as_tenant_admin_403(admin_tok, tenant_a):
    """登录安全仅超管可读写"""
    r = requests.get(f"{BASE}/api/admin/login-config",
                     headers=H(tenant_a["ta"][2]), timeout=15)
    assert r.status_code == 403
    r = requests.put(f"{BASE}/api/admin/login-config", headers=H(tenant_a["ta"][2]),
                     timeout=15, json={"user_attempts": 5})
    assert r.status_code == 403

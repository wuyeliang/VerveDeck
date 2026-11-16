# -*- coding: utf-8 -*-
"""集群时间配置 API（时区 + NTP）：GET 结构 / 非法参数 400 / probe 结构 / RBAC

非破坏性约束：本文件不真实应用宿主时区、不写宿主 NTP 配置
（真实 apply 链路走部署验证清单手工执行，避免污染共享测试环境）。
"""
import pytest
import requests
from fastapi import HTTPException

from conftest import BASE, H
from app.api.admin import _parse_ntp_servers


def _get(admin_tok):
    r = requests.get(f"{BASE}/api/admin/time-config", headers=H(admin_tok), timeout=30)
    r.raise_for_status()
    return r.json()


def _put(admin_tok, **body):
    return requests.put(f"{BASE}/api/admin/time-config", headers=H(admin_tok),
                        timeout=30, json=body)


def test_get_shape(admin_tok):
    d = _get(admin_tok)
    assert {"timezone", "ntp_servers", "source", "updated_at", "epoch", "host"} <= set(d)
    assert isinstance(d["ntp_servers"], list)
    assert d["source"] in ("db", "host")
    host = d["host"]
    assert host["timezone"]
    assert host["now"]
    assert {"impl", "file", "configured"} <= set(host["ntp"])
    assert isinstance(host["ntp"]["configured"], list)


def test_put_invalid_timezone_400(admin_tok):
    assert _put(admin_tok, timezone="Not/AZone").status_code == 400
    assert _put(admin_tok, timezone="Asia/").status_code == 400


def test_put_invalid_ntp_400(admin_tok):
    # 非法主机 / 非法端口 / 超上限，均在 apply 之前 400，不触碰宿主
    assert _put(admin_tok, timezone="", ntp_servers="bad host!").status_code == 400
    assert _put(admin_tok, timezone="", ntp_servers=["10.0.0.5:99999"]).status_code == 400
    assert _put(admin_tok, timezone="", ntp_servers=["10.0.0.5:0"]).status_code == 400
    assert _put(admin_tok, timezone="",
                ntp_servers=[f"10.0.0.{i}" for i in range(1, 11)]).status_code == 400


def test_probe_empty_400(admin_tok):
    r = requests.post(f"{BASE}/api/admin/time-config/probe", headers=H(admin_tok),
                      timeout=15, json={"timezone": "", "ntp_servers": []})
    assert r.status_code == 400


def test_probe_shape(admin_tok):
    """本地不可达端口探测：保证返回结构（ok=false+detail），不出公网"""
    r = requests.post(f"{BASE}/api/admin/time-config/probe", headers=H(admin_tok),
                      timeout=60, json={"timezone": "", "ntp_servers": ["127.0.0.1:1"]})
    r.raise_for_status()
    results = r.json()["results"]
    assert len(results) == 1
    item = results[0]
    assert item["server"] == "127.0.0.1:1"
    assert item["ok"] in (True, False)
    if item["ok"]:
        assert isinstance(item["offset_ms"], (int, float))
    else:
        assert item["detail"]


def test_get_as_tenant_admin_403(admin_tok, tenant_a):
    """时间配置仅超管可读写"""
    r = requests.get(f"{BASE}/api/admin/time-config",
                     headers=H(tenant_a["ta"][2]), timeout=15)
    assert r.status_code == 403
    r = requests.put(f"{BASE}/api/admin/time-config", headers=H(tenant_a["ta"][2]),
                     timeout=15, json={"timezone": "Asia/Shanghai"})
    assert r.status_code == 403


def test_parse_ntp_servers_normalized():
    """输入归一化：字符串混合分隔符（含空格）/ 去 scheme / 去重"""
    assert _parse_ntp_servers("ntp.a.com, 10.0.0.5:123\nntp://ntp.b.com; ntp.a.com") \
        == ["ntp.a.com", "10.0.0.5:123", "ntp.b.com"]
    assert _parse_ntp_servers("ntp.a.com 10.0.0.5:234") == ["ntp.a.com", "10.0.0.5:234"]
    assert _parse_ntp_servers([]) == []
    assert _parse_ntp_servers("") == []
    assert _parse_ntp_servers('http://ntp.c.com:323') == ["ntp.c.com:323"]


def test_parse_ntp_servers_bad_input():
    with pytest.raises(HTTPException) as e:
        _parse_ntp_servers("bad!host")
    assert e.value.status_code == 400
    with pytest.raises(HTTPException):
        _parse_ntp_servers("10.0.0.5:abc")
    with pytest.raises(HTTPException):
        _parse_ntp_servers([f"10.0.0.{i}" for i in range(9)])  # 9 > 8

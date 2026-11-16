# -*- coding: utf-8 -*-
"""TC-GATEWAY 网关限流页面配置（系统管理→设置→网关限流）。

PUT 保存链路 = 渲染 zones.conf → 写 web 容器 → nginx -t → reload（失败回滚）。
本地无 docker/web 容器时 applied.ok=False 属预期（配置仍落库，启动自愈会补应用），
只断言落库回读与校验语义；热生效链路在服务器 E2E 验证。
"""
import pytest
import requests

from conftest import BASE, H, SUF


# ---------------- render_zones 纯函数（渲染正确性 + 注入防护） ----------------

def test_render_zones_content():
    from app.services import gateway_service as gs
    out = gs.render_zones(7, 40, 80, 100)
    assert "rate=7r/s" in out and "zone=login_limit" in out
    assert "rate=40r/s" in out and "zone=api_limit" in out
    assert "rate=80r/s" in out and "zone=v1_limit" in out
    assert "limit_conn conn_limit 100;" in out
    assert "limit_conn_zone" in out


@pytest.mark.parametrize("bad", ["5; rm -rf /", "-3", "abc", "", "1.5e3"])
def test_render_zones_rejects_injection(bad):
    from app.services import gateway_service as gs
    with pytest.raises(ValueError):
        gs.render_zones(bad, 30, 60, 50)


# ---------------- API：权限 / 校验 / 保存回读 ----------------

def test_gateway_rate_requires_admin():
    """网关限流为超管专属（未认证 401）"""
    assert requests.get(f"{BASE}/api/admin/gateway-rate", timeout=15).status_code == 401
    r = requests.put(f"{BASE}/api/admin/gateway-rate",
                     json={"login_rate": 5, "api_rate": 30, "v1_rate": 60, "conn_limit": 50},
                     timeout=15)
    assert r.status_code == 401


def test_gateway_rate_put_validation(admin_tok):
    """范围校验：越界 400，合法值放行"""
    for body, msg in [
        ({"login_rate": 0, "api_rate": 30, "v1_rate": 60, "conn_limit": 50}, "登录接口限流"),
        ({"login_rate": 1, "api_rate": 0, "v1_rate": 60, "conn_limit": 50}, "API 接口限流"),
        ({"login_rate": 1, "api_rate": 30, "v1_rate": 99999, "conn_limit": 50}, "OpenAI 兼容接口限流"),
        ({"login_rate": 1, "api_rate": 30, "v1_rate": 60, "conn_limit": 99999}, "单 IP 并发连接"),
    ]:
        r = requests.put(f"{BASE}/api/admin/gateway-rate", json=body,
                         headers=H(admin_tok), timeout=15)
        assert r.status_code == 400, f"{body} 应 400: {r.text[:200]}"


def test_gateway_rate_save_and_readback(admin_tok):
    """PUT 保存 → 回读一致（本地无 docker 时 applied.ok=False 属预期，不断言）"""
    body = {"login_rate": 7, "api_rate": 40, "v1_rate": 80, "conn_limit": 100}
    r = requests.put(f"{BASE}/api/admin/gateway-rate", json=body,
                     headers=H(admin_tok), timeout=30)
    assert r.status_code == 200, f"保存应 200: {r.text[:300]}"
    data = r.json()
    assert "applied" in data and "detail" in data["applied"]
    for k, v in body.items():
        assert data["config"][k] == v, f"{k} 回读应为 {v}: {data['config']}"
    assert data["config"]["source"] == "db"
    # 独立 GET 再确认持久化
    r = requests.get(f"{BASE}/api/admin/gateway-rate", headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    for k, v in body.items():
        assert r.json()[k] == v

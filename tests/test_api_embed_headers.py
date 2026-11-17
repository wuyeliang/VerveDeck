"""网关嵌入头（nginx 层）：监控大屏 Grafana iframe 同源嵌入必须可渲染。

回归背景（2026-11-17 公网事故）：安全加固在 server 级加了
X-Frame-Options DENY，nginx add_header 继承规则（location 内无任何
add_header 时继承外层）把它带进 /grafana/ /grafana-light/ 代理响应，
浏览器（Firefox/Chrome）拒绝渲染大屏 iframe（「无法打开此页面……
将不允许显示嵌入了其他网站的页面」）。既有套件全部直连 backend 3001、
不经 nginx，网关层响应头回归无人覆盖——本文件补这个缺口。

直连 web 入口（自签证书 verify=False，自动探测 443 / 8433 自定义端口）。
"""
import pytest
import requests
import urllib3

urllib3.disable_warnings()


def _find_web_base() -> str | None:
    for port in (443, 8433):
        base = f"https://127.0.0.1:{port}"
        try:
            r = requests.get(base, verify=False, timeout=5, allow_redirects=False)
            if r.status_code in (200, 301):
                return base
        except requests.RequestException:
            continue
    return None


WEB_BASE = _find_web_base()

pytestmark = pytest.mark.skipif(
    WEB_BASE is None, reason="web 入口（nginx）不可达，跳过网关嵌入头测试")


def _assert_embed_ok(r: requests.Response, prefix: str):
    assert r.status_code == 200, f"{prefix} 应 200，实际 {r.status_code}"
    xfo = r.headers.get("X-Frame-Options")
    assert xfo != "DENY", (
        f"{prefix} 携带 X-Frame-Options: DENY——浏览器会拒绝渲染嵌入页面"
        "（server 级安全头被 nginx 继承进代理响应；该 location 需显式重声明"
        "保留的安全头以切断继承）")
    # 切断继承后其余安全头仍须保留（不能一刀切全丢）。
    # 注：Grafana 自身也发 nosniff，与 nginx 重声明叠加会出现
    # "nosniff, nosniff" 重复头（同值无害），故断言按值包含而非全等
    assert "Strict-Transport-Security" in r.headers, f"{prefix} 丢失 HSTS"
    xcto = r.headers.get("X-Content-Type-Options") or ""
    assert "nosniff" in [v.strip() for v in xcto.split(",")], f"{prefix} 丢失 nosniff"


def test_spa_document_deny_embed():
    """SPA 主文档应保持 DENY（防平台页被第三方嵌入）——安全加固意图不受影响"""
    r = requests.get(f"{WEB_BASE}/", verify=False, timeout=15)
    assert r.status_code == 200
    assert r.headers.get("X-Frame-Options") == "DENY"


def test_grafana_embed_allowed():
    r = requests.get(f"{WEB_BASE}/grafana/", verify=False, timeout=15)
    _assert_embed_ok(r, "/grafana/")


def test_grafana_light_embed_allowed():
    r = requests.get(f"{WEB_BASE}/grafana-light/", verify=False, timeout=15)
    if r.status_code == 404:
        pytest.skip("grafana-light 未部署（旧版 compose）")
    _assert_embed_ok(r, "/grafana-light/")

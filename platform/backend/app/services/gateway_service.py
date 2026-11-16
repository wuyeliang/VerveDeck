"""网关限流服务：渲染 nginx zone 配置 + 热加载到 web 容器。

链路：render_zones() 纯函数渲染 → 经 docker exec 写入 web 容器的
/etc/nginx/conf.d/00-zones.conf（compose 绑定挂载宿主 deploy/nginx/00-zones.conf，
写容器即落宿主，容器重建不丢）→ nginx -t 校验 → nginx -s reload 热加载
（新 worker 接管，存量连接不中断）。

安全边界：
- 四个值只接受正整数（API 层范围校验 + 本模块二次数字校验，nginx 配置无注入面）；
- nginx -t 失败立即回滚原文件内容并报错，绝不让坏配置上线；
- web 容器不可达（本地开发/无 docker）时仅保存 DB 并如实报告，
  backend 启动时的自愈重放（main.py）会补齐应用。
"""
import base64
import logging
import re

log = logging.getLogger("gateway")

ZONE_PATH = "/etc/nginx/conf.d/00-zones.conf"
WEB_LABEL = "com.docker.compose.service=web"
WEB_NAME_FALLBACK = "fintune-web-1"


def _check_int(name: str, v) -> int:
    i = int(v)
    if i < 1 or not re.fullmatch(r"[0-9]+", str(i)):
        raise ValueError(f"{name} 需为正整数")
    return i


def render_zones(login_rate, api_rate, v1_rate, conn_limit) -> str:
    """渲染 00-zones.conf 全文（http 上下文：zone 定义 + limit_conn 生效行）。
    速率单位 r/s；burst 缓冲固定在 default.conf（登录 5 / API 60 / v1 120）。"""
    lr = _check_int("login_rate", login_rate)
    ar = _check_int("api_rate", api_rate)
    vr = _check_int("v1_rate", v1_rate)
    cl = _check_int("conn_limit", conn_limit)
    return (
        "# VerveDeck 网关限流 —— 平台自动生成（系统管理→设置→网关限流），勿手改\n"
        "limit_req_zone $binary_remote_addr zone=login_limit:10m rate=%dr/s;\n"
        "limit_req_zone $binary_remote_addr zone=api_limit:10m rate=%dr/s;\n"
        "limit_req_zone $binary_remote_addr zone=v1_limit:10m rate=%dr/s;\n"
        "limit_conn_zone $binary_remote_addr zone=conn_limit:10m;\n"
        "limit_conn conn_limit %d;     # 单 IP 并发连接上限\n"
    ) % (lr, ar, vr, cl)


def _find_web_container(d):
    """定位 web 容器：优先 compose 服务标签，回退固定容器名。找不到返回 None。"""
    try:
        ctrs = d.containers.list(filters={"label": WEB_LABEL})
        if ctrs:
            return ctrs[0]
    except Exception:
        pass
    try:
        return d.containers.get(WEB_NAME_FALLBACK)
    except Exception:
        return None


def apply_zones(content: str) -> dict:
    """写 zones.conf 到 web 容器并热加载。返回 {ok, applied_now, detail}。
    nginx -t 失败自动回滚原内容。任何环节失败均不抛异常（配置已存 DB）。"""
    try:
        from .taskcontainer import docker_client, exec_run
        d = docker_client()
    except Exception as e:
        return {"ok": False, "applied_now": False,
                "detail": f"Docker 未连接，配置已保存但未热生效（{e.__class__.__name__}）"}
    ctr = _find_web_container(d)
    if ctr is None:
        return {"ok": False, "applied_now": False,
                "detail": "未找到 web 容器（fintune-web），配置已保存，web 重建后由启动自愈应用"}
    try:
        # 原内容（回滚用）
        code, old = exec_run(ctr.name, ["sh", "-c", f"cat {ZONE_PATH}"], timeout=15)
        # base64 写入（规避 shell 转义）
        b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
        code, out = exec_run(ctr.name,
                             ["sh", "-c", f"echo {b64} | base64 -d > {ZONE_PATH} && echo WROTE"],
                             timeout=15)
        if code != 0 or "WROTE" not in out:
            return {"ok": False, "applied_now": False,
                    "detail": f"写入 {ZONE_PATH} 失败：{out.strip()[-200:]}"}
        # 先校验后重载；校验失败立即回滚原内容
        code, out = exec_run(ctr.name, ["nginx", "-t"], timeout=30)
        if code != 0:
            if old:
                b64old = base64.b64encode(old.encode("utf-8")).decode("ascii")
                exec_run(ctr.name, ["sh", "-c", f"echo {b64old} | base64 -d > {ZONE_PATH}"],
                         timeout=15)
            return {"ok": False, "applied_now": False,
                    "detail": f"nginx -t 校验失败，已回滚原配置：{out.strip()[-300:]}"}
        code, out = exec_run(ctr.name, ["nginx", "-s", "reload"], timeout=30)
        if code != 0:
            return {"ok": False, "applied_now": False,
                    "detail": f"nginx reload 失败：{out.strip()[-200:]}"}
        return {"ok": True, "applied_now": True, "detail": "限流配置已热生效（nginx reload，连接不中断）"}
    except Exception as e:
        log.warning("apply_zones failed: %s", e)
        return {"ok": False, "applied_now": False,
                "detail": f"应用失败：{e.__class__.__name__}: {str(e)[:150]}"}

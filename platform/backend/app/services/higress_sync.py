"""网关路由同步器（控制面 → 数据面，调度器 tick 驱动）。

把 DB 里的模型路由（权重/容灾/目标部署状态）收敛到 Higress 网关：

  nginx:8443 /v1/ → Higress:8080
    ├─ vd-catchall   PRE /v1/         → backend:3001（存量直连兼容：
    │      /v1/<部署名>/ 的旧入口、以及未入路由的部署，全走这条兜底）
    └─ vd-r<路由id>  PRE /v1/<名>/    → backend:<gateway_port> 加权
           （每部署一个内部目标代理端口：认证+护栏+PD 转发完整保留）

容灾语义：
- 主目标（is_fallback=False）任一可用 → 路由只到主目标，权重归一化到 100；
- 主目标全不可用 → fallback 目标权重归一化到 100 写回网关（自动接管）；
  主目标恢复后下一 tick 自动还原；
- 路由级 proxyNextUpstream 重试（attempts=2，5xx/连接错误）：多端点时
  单实例 5xx 直接换端点，秒级故障切换，不依赖 reconciler 周期。

同步策略：
- 幂等 upsert（PUT）+ 进程内 payload 哈希去重（无变化不打 API）；
- 孤儿清理：vd- 前缀路由/服务源在 DB 无对应时删除（路由禁用=摘除流量）；
- Console 不可达/认证失败只记 sync_status=error（限频），不阻塞其他 reconcile。
"""
import hashlib
import json
import logging
import time

from ..config import settings
from ..models import Deployment, ModelRoute, ModelRouteTarget
from . import higress_client as hc
from . import port_pool

log = logging.getLogger("higress_sync")

CATCHALL_ROUTE = "vd-catchall"
CATCHALL_SOURCE = "vd-backend"
_BACKEND_PORT = 3001
# 同步失败时 DB 回写限频（防 tick 级刷屏）
_STATUS_WRITE_MIN_GAP = 30.0

_applied: dict[str, str] = {}      # 路由名 → payload 哈希
_applied_sources: dict[str, str] = {}  # 服务源名 → payload 哈希
_last_status_write: dict[int, float] = {}  # 路由 id → 上次 error 回写时刻


def _route_higress_name(route: ModelRoute) -> str:
    return f"{hc.NAME_PREFIX}r{route.id}"


def _source_name(dep_id: int) -> str:
    return f"{hc.NAME_PREFIX}dp{dep_id}"


def _dns_domain(host: str) -> str:
    """网关服务源域名 = <docker 服务名>.local。

    Console 的域名格式校验不接受裸主机名（backend）与含下划线的标签
    （fintune_net），而 docker 服务名/网络名恰属这两类；`<name>.local`
    作为 compose 网络别名（backend 服务 aliases）注册后，内置 DNS 可
    解析到服务容器，Envoy 端 DNS 解析同路径。
    """
    return f"{host}.local"


def _dns_source(name: str, host: str, port: int) -> dict:
    """DNS 类型服务源：域名解析走 docker 内置 DNS（容器重建换 IP 无感）。"""
    return {"name": name, "type": "dns", "domain": _dns_domain(host),
            "port": port, "protocol": "http"}


def _hash(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _normalize_weights(items: list) -> list:
    """[(dep, raw_weight)] → 服务列表（整数权重，和恒 = 100）。"""
    total = sum(max(0, int(w or 0)) for _, w in items)
    if total <= 0:
        # 权重全 0/缺省：等分
        n = len(items)
        base, rem = 100 // n, 100 % n
        ws = [base + (1 if i < rem else 0) for i in range(n)]
    else:
        ws = [max(1, round(100 * max(0, int(w or 0)) / total)) for _, w in items]
        # 修正取整误差到最大权重项，保证和 = 100
        diff = 100 - sum(ws)
        if diff != 0:
            mi = max(range(len(ws)), key=lambda i: ws[i])
            ws[mi] = max(1, ws[mi] + diff)
    return ws


def _find_service(services: list, host: str, port: int) -> dict | None:
    """服务源注册后的服务名发现（命名规则随版本可能变化，动态匹配最稳）。"""
    for ep in (f"{_dns_domain(host)}:{port}", f"{host}:{port}"):
        for s in services:
            if ep in (s.get("endpoints") or []):
                return s
    for s in services:
        if s.get("name") == host and (s.get("port") in (None, port)):
            return s
    return None


def _ensure_dns_service(c: hc.HigressClient, host: str, port: int,
                        source_name: str) -> dict | None:
    """确保 DNS 服务源存在并返回解析出的服务条目（未就绪返回 None）。"""
    src = _dns_source(source_name, host, port)
    h = _hash(src)
    if _applied_sources.get(source_name) != h:
        c.upsert_service_source(src)
        _applied_sources[source_name] = h
    svc = _find_service(c.list_services(), host, port)
    return svc


def _target_app():
    """内部目标代理 ASGI app（inference 模块构建；惰性导入防循环）。"""
    try:
        from ..api.inference import target_app
    except ImportError as e:
        raise RuntimeError(f"内部目标代理 app 未就绪: {e}") from e
    return target_app


def _ensure_upstream(c: hc.HigressClient, dep: Deployment,
                     reserved_ports: set) -> dict:
    """部署 → 网关上游：分配端口 → 起目标代理 → 注册服务源 → 发现服务名。

    reserved_ports：DB 已分配端口（含本 tick 刚分配的），防池内端口被重复挑选。
    分配成功后调用方负责 db.commit() 持久化 gateway_port（重启后按此恢复）。
    """
    pool = port_pool.pool()
    if not dep.gateway_port:
        dep.gateway_port = pool.free_port(reserved_ports)
        if not dep.gateway_port:
            raise RuntimeError(f"端口池耗尽（{settings.gateway_port_min}-"
                               f"{settings.gateway_port_max} 已占满）")
        reserved_ports.add(dep.gateway_port)
    if not pool.start(dep.gateway_port, _target_app()):
        raise RuntimeError(f"端口 {dep.gateway_port} 启动失败（池外或被占用）")
    svc = _ensure_dns_service(c, settings.higress_upstream_host,
                              dep.gateway_port, _source_name(dep.id))
    if svc is None:
        raise RuntimeError(f"服务 {settings.higress_upstream_host}:"
                           f"{dep.gateway_port} 尚未在网关就绪（下个周期重试）")
    return {"name": svc.get("name"), "port": svc.get("port") or dep.gateway_port}


def _retries() -> dict:
    return {"proxyNextUpstream": {
        "enabled": True,
        "attempts": 2,
        "conditions": ["error", "http_500", "http_502", "http_503", "http_504"],
    }}


def _internal_stamp() -> dict:
    """网关转发前注入内部令牌（目标代理据此拒绝网络内旁路直调）。"""
    return {"headerControl": {
        "enabled": True,
        "request": {"add": [],
                    "set": [{"key": "x-vervedeck-internal",
                             "value": settings.target_proxy_token}],
                    "remove": []},
        "response": {"add": [], "set": [], "remove": []},
    }}


def _ensure_catchall(c: hc.HigressClient) -> None:
    """存量兼容兜底路由：/v1/ 前缀 → backend:3001（最长前缀匹配让具体路由优先）。"""
    svc = _ensure_dns_service(c, settings.higress_upstream_host,
                              _BACKEND_PORT, CATCHALL_SOURCE)
    if svc is None:
        raise RuntimeError("backend 主端口服务未在网关就绪（下个周期重试）")
    route = {
        "name": CATCHALL_ROUTE,
        "domains": ["*"],
        "path": {"matchType": "PRE", "matchValue": "/v1/"},
        "services": [{"name": svc.get("name"),
                      "port": svc.get("port") or _BACKEND_PORT,
                      "weight": 100}],
    }
    h = _hash(route)
    if _applied.get(CATCHALL_ROUTE) != h:
        c.upsert_route(route)
        _applied[CATCHALL_ROUTE] = h


def _healthy_targets(route: ModelRoute, targets, deployments) -> list:
    """容灾决策：主目标可用 → 只用主目标；否则 fallback 接管。返回 [(dep, weight)]。"""
    deps = {d.id: d for d in deployments}
    primary = [(deps[t.deployment_id], t.weight) for t in targets
               if not t.is_fallback and t.deployment_id in deps
               and deps[t.deployment_id].status == "running"]
    if primary:
        return primary
    fallback = [(deps[t.deployment_id], t.weight) for t in targets
                if t.is_fallback and t.deployment_id in deps
                and deps[t.deployment_id].status == "running"]
    return fallback


def _sync_one_route(c: hc.HigressClient, db, route: ModelRoute) -> None:
    targets = db.query(ModelRouteTarget).filter_by(route_id=route.id).all()
    dep_ids = {t.deployment_id for t in targets}
    deployments = (db.query(Deployment)
                   .filter(Deployment.id.in_(dep_ids)).all() if dep_ids else [])
    name = _route_higress_name(route)
    fallback_ids = {t.deployment_id for t in targets if t.is_fallback}

    if not route.enabled or not targets:
        # 禁用/无目标：摘除网关路由（流量落 catch-all → 后端返回部署不存在）
        try:
            c.delete_route(name)
            _applied.pop(name, None)
        except hc.HigressError:
            pass
        _set_status(db, route, "synced", "已停用" if not route.enabled else "无目标")
        return

    upstream = _healthy_targets(route, targets, deployments)
    if not upstream:
        try:
            c.delete_route(name)
            _applied.pop(name, None)
        except hc.HigressError:
            pass
        _set_status(db, route, "synced", "无可用目标（主目标与容灾目标均不可用）")
        return

    # 端口池占用全集（DB 已分配的），防本 tick 内多路由重复挑选同一端口
    reserved = {d.gateway_port for d in db.query(Deployment).all()
                if d.gateway_port}
    services, ws = [], _normalize_weights(upstream)
    for (dep, _w), weight in zip(upstream, ws):
        up = _ensure_upstream(c, dep, reserved)
        services.append({**up, "weight": weight})

    payload = {
        "name": name,
        "domains": ["*"],
        "path": {"matchType": "PRE", "matchValue": f"/v1/{route.name}/"},
        "services": services,
        **_retries(),
        **_internal_stamp(),
    }
    h = _hash(payload)
    if _applied.get(name) != h:
        c.upsert_route(payload)
        _applied[name] = h
    _set_status(db, route, "synced",
                f"{len(services)} 个端点加权" +
                ("（容灾接管中：主目标不可用）"
                 if all(d.id in fallback_ids for d, _ in upstream) else ""))


def _set_status(db, route: ModelRoute, status: str, message: str) -> None:
    if route.sync_status == status and route.sync_message == message:
        return
    if status == "error":
        now = time.time()
        if now - _last_status_write.get(route.id, 0.0) < _STATUS_WRITE_MIN_GAP:
            return
        _last_status_write[route.id] = now
    route.sync_status = status
    route.sync_message = message[:512]


def _gc_orphans(c: hc.HigressClient, db, wanted_routes: set) -> None:
    """清理 DB 已删/停用的 vd- 前缀孤儿路由与服务源。"""
    try:
        for r in c.list_routes():
            rn = r.get("name") or ""
            if not rn.startswith(hc.NAME_PREFIX) or rn == CATCHALL_ROUTE:
                continue
            if rn not in wanted_routes:
                c.delete_route(rn)
                _applied.pop(rn, None)
        for s in c.list_service_sources():
            sn = s.get("name") or ""
            if sn.startswith(hc.NAME_PREFIX) and sn != CATCHALL_SOURCE \
                    and sn not in _applied_sources:
                c.delete_service_source(sn)
                _applied_sources.pop(sn, None)
    except hc.HigressError as e:
        log.warning("orphan gc: %s", e)


def sync_routes(db) -> None:
    """调度器 tick 入口：DB 路由状态 → Higress 收敛（整体容错，不影响其他 reconcile）。"""
    if not settings.higress_enabled:
        return
    c = hc.client()
    routes = db.query(ModelRoute).all()
    if not c.ping():
        for r in routes:
            _set_status(db, r, "error", "网关控制面不可达（检查 higress 容器/凭据）")
        try:
            db.commit()
        except Exception:
            db.rollback()
        return
    try:
        _ensure_catchall(c)
        wanted = set()
        for route in routes:
            try:
                _sync_one_route(c, db, route)
                wanted.add(_route_higress_name(route))
            except hc.HigressError as e:
                _set_status(db, route, "error", str(e)[:200])
                log.warning("route %s sync failed: %s", route.name, e)
            except Exception as e:  # noqa: BLE001  单路由异常不拖垮整体
                _set_status(db, route, "error", str(e)[:200])
                log.exception("route %s sync error", route.name)
        _gc_orphans(c, db, wanted)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("higress sync failed")

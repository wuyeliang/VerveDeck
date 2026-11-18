"""内部目标代理端口池（默认 3100-3199）。

每部署一个端口：在该端口起一个 uvicorn Server 线程，承载「内部目标代理」
ASGI app（认证 + 护栏 + PD 二阶段转发，复用 openai_proxy 核心）。Higress 路由
的加权端点即指向这些端口（Envoy 端点粒度 = 部署粒度，权重/重试/容灾全在
网关侧，backend 主端口 3001 不再承担按目标转发）。

生命周期：
- start(port, app)：幂等（已运行直接返回）；app 惰性注入（target_app 由
  inference 模块构建，避免循环导入）。
- stop(port)：置 should_exit 让 Server 线程自行退出并 join。
- free_port(reserved)：在 [min,max] 内挑一个未运行且未被 reserved 占用的端口。
- restore(reservations)：backend 重启后按 DB 持久化的 gateway_port 恢复服务
  （reservations = {port: app_factory}，由同步器在启动时驱动）。

安全边界：端口池只监听 0.0.0.0:31xx（docker 网络内），compose 不把这批端口
映射到宿主公网；目标代理自身强制内部令牌 + 用户认证（见 inference.target_app）。
"""
import logging
import threading

import uvicorn

from ..config import settings

log = logging.getLogger("port_pool")


class PortPool:
    """uvicorn Server 线程池（每端口一个 asyncio 事件循环线程）。"""

    def __init__(self, port_min: int = 0, port_max: int = 0):
        self.min = port_min or settings.gateway_port_min
        self.max = port_max or settings.gateway_port_max
        self._lock = threading.Lock()
        # port -> (uvicorn.Server, threading.Thread, app 对象)
        self._servers: dict[int, tuple] = {}

    # ---------- 生命周期 ----------

    def start(self, port: int, app) -> bool:
        """在 port 起（或确认已起）目标代理服务。成功/已运行返回 True。"""
        if not (self.min <= port <= self.max):
            log.warning("port out of pool range: %d (range %d-%d)", port, self.min, self.max)
            return False
        with self._lock:
            if port in self._servers:
                return True
            config = uvicorn.Config(app=app, host="0.0.0.0", port=port,
                                    log_level="warning", lifespan="off")
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run,
                                      name=f"target-proxy-{port}", daemon=True)
            thread.start()
            self._servers[port] = (server, thread, app)
        log.info("target proxy port up: %d", port)
        return True

    def stop(self, port: int) -> None:
        with self._lock:
            entry = self._servers.pop(port, None)
        if entry is None:
            return
        server, thread, _ = entry
        try:
            server.should_exit = True
            thread.join(timeout=3)
        except Exception as e:  # noqa: BLE001  退出路径不抛
            log.warning("stop port %d: %s", port, str(e)[:120])
        log.info("target proxy port down: %d", port)

    def running(self) -> set:
        with self._lock:
            return set(self._servers.keys())

    def free_port(self, reserved: set | None = None) -> int | None:
        """挑一个空闲端口（未运行 + 不在 reserved——DB 已分配给部署的）。"""
        reserved = {int(p) for p in (reserved or set())}
        with self._lock:
            for p in range(self.min, self.max + 1):
                if p not in self._servers and p not in reserved:
                    return p
        return None

    def stop_all(self) -> None:
        for p in list(self.running()):
            self.stop(p)


_pool: PortPool | None = None
_pool_lock = threading.Lock()


def pool() -> PortPool:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = PortPool()
        return _pool

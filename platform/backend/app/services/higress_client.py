"""Higress Console API 客户端（推理网关控制面）。

数据面：Higress 网关（docker 网络内 8080）承载模型路由的加权/容灾/重试；
控制面：本模块经 Console API（8001，仅 docker 网络可达）CRUD 路由与服务源，
由 gateway 同步器（higress_sync）在调度器 tick 中驱动，把 DB 里的
路由/目标/部署状态收敛到网关。

Console API 约定（v2.2.6 源码核实）：
- 认证：session-cookie（Basic Auth 默认关闭）。POST /session/login
  {username, password} → Set-Cookie: _hi_sess；会话空闲 30min/绝对 8h 过期，
  本客户端遇 401 自动重新登录续期。
- 首次初始化：Console 首启无管理员（system.initialized=false），登录报
  "No valid admin config is available"；本客户端自动调 POST /system/init
  {adminUser: {name, password, displayName}} 创建管理员（Console 将其写入
  自身 secret 持久化到 /data 卷，重启不丢；已初始化则幂等忽略）。
  注意登录失败预算：失败次数超限会锁定（LoginRateLimitException）——
  密码错误时退避 60s 不重试，避免把预算打穿导致人工登录也被锁。
- 响应包裹：{"success": bool, "message": str, "data": <T>}；
  分页列表 data 再嵌一层 {data: [...], pageNum, pageSize, total}。
- 写语义：POST 仅创建（重名 409）；PUT /v1/{kind}/{name} 为 upsert 更新；
  DELETE 204。
- 路由模型：Route{name, domains[], path:{matchType: EQUAL|PRE|REGULAR,
  matchValue}, services:[{name, port?, weight?}], retries?:{attempt, retryOn,
  enabled}}；同路由 services 权重和必须 = 100（控制台校验，Envoy 层为相对权重）。
- 服务源模型：ServiceSource{name, type: static|dns|..., domain, port,
  protocol}；static 的 domain 是 ip:port 逗号列表（端口写 80），dns 的 domain
  是主机名（docker 服务名即主机名），端口为真实端口。
"""
import logging
import re
import threading
import time

import httpx

from ..config import settings

log = logging.getLogger("higress_client")

# 平台资源命名前缀（孤儿清理/归属识别用；勿与其他前缀混用）
NAME_PREFIX = "vd-"

# 会话 cookie 名（Console 属性 higress-console.admin.cookie.name 默认值）
_SESSION_COOKIE = "_hi_sess"

# 认证失败退避（防打穿 Console 登录失败预算）
_AUTH_BACKOFF_SEC = 60.0
# 连接不可达退避（Console 容器未起/网络未通）
_CONN_BACKOFF_SEC = 10.0


class _NotInitialized(Exception):
    """Console 尚无管理员账户（首次部署未走初始化向导）"""


class HigressError(Exception):
    """Console API 调用失败（带 HTTP 状态码与消息）"""

    def __init__(self, status: int, message: str):
        super().__init__(f"[{status}] {message}")
        self.status = status
        self.message = message


def sanitize_name(name: str, prefix: str = NAME_PREFIX, max_len: int = 63) -> str:
    """Higress 资源名：小写字母/数字/连字符，1-63 字符，不以连字符开头结尾。"""
    s = re.sub(r"[^a-z0-9-]+", "-", (name or "").lower()).strip("-")
    s = re.sub(r"-{2,}", "-", s)
    if not s:
        s = "unnamed"
    return (prefix + s)[:max_len].rstrip("-")


class HigressClient:
    """Console API 薄封装（线程安全：每次调用独立 httpx.Client）。

    共享状态：会话 cookie（401 自动重登续期）+ 两个退避时间戳（进程内，
    重启清零，幂等无副作用）。
    """

    def __init__(self, base_url: str = "", username: str = "", password: str = ""):
        self.base_url = (base_url or settings.higress_console_url).rstrip("/")
        self.username = username or settings.higress_console_username
        self.password = password or settings.higress_console_password
        self._lock = threading.Lock()
        self._session_cookie: str | None = None
        self._auth_failed_until = 0.0
        self._conn_failed_until = 0.0

    # ---------- 认证 ----------

    def _login(self) -> str:
        """会话登录，返回 cookie 值。未初始化先自动建管理员账户再登录。"""

        def _do_login() -> str:
            with httpx.Client(timeout=8.0) as c:
                r = c.post(self.base_url + "/session/login", json={
                    "username": self.username, "password": self.password})
            # 成功 = 200/201 且下发会话 cookie（成功响应体为裸用户对象，
            # 无 success 包裹；201 为实测状态码）
            if r.status_code in (200, 201):
                cookie = r.cookies.get(_SESSION_COOKIE)
                if cookie:
                    return cookie
            try:
                payload = r.json()
            except Exception:
                payload = {}
            msg = str(payload.get("message") or "")
            if "No valid admin config" in msg:
                raise _NotInitialized()
            if "Too many failed login attempts" in msg:
                raise HigressError(429, "console login rate-limited")
            raise HigressError(
                401, "console auth failed: " + (msg[:80] or r.text[:80]))

        try:
            cookie = _do_login()
        except _NotInitialized:
            self._init_system()
            cookie = _do_login()
        with self._lock:
            self._session_cookie = cookie
        log.info("higress console session established (user=%s)", self.username)
        return cookie

    def _init_system(self) -> None:
        """首次初始化：创建管理员账户（写入 Console 自身 secret 持久化；
        已初始化时幂等忽略）。"""
        with httpx.Client(timeout=8.0) as c:
            r = c.post(self.base_url + "/system/init", json={
                "adminUser": {"name": self.username,
                              "password": self.password,
                              "displayName": "Admin"}})
        try:
            payload = r.json()
        except Exception:
            payload = {}
        if payload.get("success"):
            log.info("higress console admin account initialized (user=%s)",
                     self.username)
            return
        msg = str(payload.get("message") or "")
        if "already initialized" in msg:
            return  # 已有管理员（凭据非本配置）：交由登录失败退避处理
        raise HigressError(r.status_code, "console init failed: " + msg[:120])

    def _ensure_session(self) -> str:
        """返回可用 cookie；无会话则登录（登录失败抛 HigressError 并退避）。"""
        with self._lock:
            if self._session_cookie:
                return self._session_cookie
        try:
            return self._login()
        except (HigressError, _NotInitialized):
            with self._lock:
                self._auth_failed_until = time.time() + _AUTH_BACKOFF_SEC
            raise

    # ---------- 基础 ----------

    def _wait_backoff(self) -> str:
        """退避检查：返回 '' 放行；否则返回原因（调用方直接跳过本次同步）"""
        now = time.time()
        if now < self._auth_failed_until:
            return "auth_backoff"
        if now < self._conn_failed_until:
            return "conn_backoff"
        return ""

    def _send(self, method: str, path: str, body, cookie: str,
              timeout: float = 8.0) -> httpx.Response:
        try:
            with httpx.Client(timeout=timeout) as client:
                return client.request(method, self.base_url + path, json=body,
                                      headers={"Content-Type": "application/json",
                                               "Cookie": f"{_SESSION_COOKIE}={cookie}"})
        except httpx.HTTPError as e:
            with self._lock:
                self._conn_failed_until = time.time() + _CONN_BACKOFF_SEC
            raise HigressError(0, f"console unreachable: {str(e)[:120]}")

    def _request(self, method: str, path: str, body=None, timeout: float = 8.0):
        """发请求并解包裹。返回 data（列表/对象/None）；失败抛 HigressError。

        401/403 = 会话过期/失效 → 作废重登后重试一次；再失败进入认证退避。
        """
        with self._lock:
            wait = self._wait_backoff()
        if wait:
            raise HigressError(0, wait)
        cookie = self._ensure_session()
        r = self._send(method, path, body, cookie, timeout)
        if r.status_code in (401, 403):
            with self._lock:
                self._session_cookie = None
            cookie = self._login()
            r = self._send(method, path, body, cookie, timeout)
            if r.status_code in (401, 403):
                with self._lock:
                    self._auth_failed_until = time.time() + _AUTH_BACKOFF_SEC
                raise HigressError(r.status_code, "console auth failed")
        if r.status_code == 404:
            raise HigressError(404, "not found")
        try:
            payload = r.json()
        except Exception:
            if 200 <= r.status_code < 300:
                return None
            raise HigressError(r.status_code, r.text[:200])
        # 包裹 {"success": bool, "message": str, "data": T}（个别端点可能裸返回）
        if isinstance(payload, dict) and "success" in payload:
            if not payload.get("success", False):
                raise HigressError(r.status_code,
                                   str(payload.get("message") or "unknown")[:200])
            return payload.get("data")
        if not (200 <= r.status_code < 300):
            raise HigressError(r.status_code, str(payload)[:200])
        return payload

    def _unwrap_list(self, data) -> list:
        """分页包裹 {data:[...], pageNum...} / 裸列表 / None 统一为 list。"""
        if data is None:
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            return data["data"]
        return []

    # ---------- 探活 ----------

    def ping(self) -> bool:
        """Console 可达且认证可用（GET /v1/routes 只读）。"""
        try:
            self._request("GET", "/v1/routes", timeout=4.0)
            return True
        except HigressError as e:
            if e.status not in (0, 401, 403):
                # 可达但接口异常（5xx 等）：视为可用，交由具体操作报错
                return True
            return False

    # ---------- 路由 ----------

    def list_routes(self) -> list:
        return self._unwrap_list(self._request("GET", "/v1/routes"))

    def get_route(self, name: str) -> dict | None:
        try:
            return self._request("GET", f"/v1/routes/{name}")
        except HigressError as e:
            if e.status == 404:
                return None
            raise

    def upsert_route(self, route: dict) -> None:
        """创建或更新路由（POST → 409 → PUT 兜底，并发安全）。"""
        try:
            self._request("POST", "/v1/routes", body=route)
        except HigressError as e:
            if e.status == 409:
                self._request("PUT", f"/v1/routes/{route['name']}", body=route)
            else:
                raise

    def delete_route(self, name: str) -> None:
        try:
            self._request("DELETE", f"/v1/routes/{name}")
        except HigressError as e:
            if e.status != 404:
                raise

    # ---------- 服务 / 服务源 ----------

    def list_services(self) -> list:
        return self._unwrap_list(self._request("GET", "/v1/services"))

    def list_service_sources(self) -> list:
        return self._unwrap_list(self._request("GET", "/v1/service-sources"))

    def upsert_service_source(self, source: dict) -> None:
        """创建或更新服务源（upsert 语义用 PUT /{name}）。"""
        self._request("PUT", f"/v1/service-sources/{source['name']}", body=source)

    def delete_service_source(self, name: str) -> None:
        try:
            self._request("DELETE", f"/v1/service-sources/{name}")
        except HigressError as e:
            if e.status != 404:
                raise


_client: HigressClient | None = None
_client_lock = threading.Lock()


def client() -> HigressClient:
    """进程内单例（配置变化场景不存在于运行期，无需重建）。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = HigressClient()
        return _client

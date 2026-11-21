"""缓存加速服务实例（托管容器，独立于推理部署）：生命周期 + 状态收敛 + 部署端点注入

容器形态与推理部署一致（pause 占位 + 容器内 exec 启动命令）；
command 留空 = 按镜像默认入口直跑（现成 KV 缓存存储镜像）。
状态机：stopped → creating（镜像准备→容器启动）→ running（健康检查通过）
        → stopped（优雅退出/人工停止）/ failed（进程非零退出，需人工 start）。
部署关联（Deployment.cache_service_id）后向其实例注入 VD_CACHE_ENDPOINT=http://ip:port。
"""
import hashlib
import json
import logging
import os
import socket
import threading
import time
import urllib.request

from ..config import settings
from ..database import SessionLocal
from ..models import CacheService, now
from . import taskcontainer as tc
from . import image_ops

log = logging.getLogger("cache-service")

# ---------- LMCache 提供方渲染（#45，参照 cache-providers.yaml 的 LMCache 供应商） ----------

_LMCACHE_IMAGE = "lmcache/vllm-openai:{version}"
_LMCACHE_DEFAULT_VERSION = "v0.5.4"
_LMCACHE_DEFAULT_PORT = 5555        # ZMQ 服务端口（--port）
_LMCACHE_DEFAULT_METRICS_PORT = 8080  # HTTP metrics/health 端口（--http-port）
# L2 后端凭据字段 → env 变量名（凭据走 env，不落命令行）
_L2_ENV_CREDENTIALS = {
    "resp": {"username": "LMCACHE_RESP_USERNAME", "password": "LMCACHE_RESP_PASSWORD"},
    "s3": {"aws_access_key_id": "AWS_ACCESS_KEY_ID",
           "aws_secret_access_key": "AWS_SECRET_ACCESS_KEY"},
}


def is_orchestrated(cs) -> bool:
    """LMCache 编排型 = config 非空。存量缓存 config 为空 → 走旧路径/逻辑型，行为不变。"""
    return bool(cs.config)


def render_spec(cs) -> dict:
    """渲染容器规格 {image, command, env, port, metrics_port, mem_gb}。

    LMCache 编排型（config 非空）：版本→镜像、chunk/淘汰/L2→启动参数、凭据→env；
    旧路径（config 空）：用顶层 image/command/env（image 空 = 逻辑型，不建容器）。"""
    cfg = cs.config or {}
    port = int(cs.port or 0) or _LMCACHE_DEFAULT_PORT
    metrics_port = int(cs.metrics_port or 0) or _LMCACHE_DEFAULT_METRICS_PORT
    if is_orchestrated(cs):
        version = (cs.provider_version or _LMCACHE_DEFAULT_VERSION).strip()
        image = (cfg.get("image") or "").strip() or _LMCACHE_IMAGE.format(version=version)
        ram = cfg.get("ram_size", 20)
        chunk = cfg.get("chunk_size", 256)
        ev_policy = cfg.get("eviction_policy", "LRU")
        ev_wm = cfg.get("eviction_trigger_watermark", 0.85)
        ev_ratio = cfg.get("eviction_ratio", 0.1)
        cmd = (
            f"lmcache server --host 0.0.0.0 --port {port} "
            f"--l1-size-gb {ram} --chunk-size {chunk} "
            f"--http-host 0.0.0.0 --http-port {metrics_port} "
            f"--supported-transfer-mode auto --worker-reap-timeout-seconds 60 "
            f"--eviction-policy {ev_policy} "
            f"--eviction-trigger-watermark {ev_wm} "
            f"--eviction-ratio {ev_ratio}"
        )
        # L2 存储级联（有序，首条优先读、全量写）：每条渲染一个 --l2-adapter JSON
        for l2 in (cfg.get("l2_storages") or []):
            if not isinstance(l2, dict):
                continue
            backend = (l2.get("backend") or "").strip()
            if not backend:
                continue
            # 凭据字段不进命令行（只经 env 注入），避免进程列表泄密
            creds = set((_L2_ENV_CREDENTIALS.get(backend) or {}).keys())
            params = {k: v for k, v in (l2.get("params") or {}).items()
                      if v not in (None, "") and k not in creds}
            params["type"] = backend
            cmd += f" --l2-adapter {json.dumps(params, ensure_ascii=False)}"
        # 额外参数（用户 flag，追加/覆盖模板默认）
        for p in (cfg.get("parameters") or []):
            if isinstance(p, str) and p.strip():
                cmd += " " + p.strip()
        env = {"PYTHONHASHSEED": "0"}
        for l2 in (cfg.get("l2_storages") or []):
            if not isinstance(l2, dict):
                continue
            mapping = _L2_ENV_CREDENTIALS.get((l2.get("backend") or "").strip())
            if not mapping:
                continue
            for field, env_name in mapping.items():
                val = (l2.get("params") or {}).get(field)
                if val not in (None, ""):
                    env[env_name] = str(val)
        env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
        try:
            mem_gb = max(8, int(ram) + 4)  # L1 + 运行开销
        except (TypeError, ValueError):
            mem_gb = 8
        return {"image": image, "command": cmd, "env": env,
                "port": port, "metrics_port": metrics_port, "mem_gb": mem_gb}
    # 旧路径 / 逻辑型
    return {"image": (cs.image or "").strip(),
            "command": (cs.command or "").strip(),
            "env": {str(k): str(v) for k, v in (cs.env or {}).items()},
            "port": int(cs.port or 0), "metrics_port": 0, "mem_gb": 8}


def spec_digest_of(cs) -> str:
    """规格摘要 = sha256(版本 + config)[:16]。漂移检测：运行中服务编辑后若摘要
    与容器启动规格不一致，须重建才生效（与 GPUStack spec_digest 同义）。"""
    payload = json.dumps({"v": cs.provider_version or "", "c": cs.config or {}},
                         sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _health_http(ip: str, port: int, path: str = "/healthcheck",
                 timeout: float = 3.0) -> tuple[bool, str]:
    """HTTP 健康检查（LMCache /healthcheck：503 直到引擎就绪，比 TCP 连接更强）。"""
    try:
        with urllib.request.urlopen(f"http://{ip}:{int(port)}{path}", timeout=timeout) as r:
            ok = 200 <= r.status < 300
            return ok, ("" if ok else f"HTTP {r.status}")
    except Exception as e:  # noqa: BLE001  连接拒绝/超时/非 2xx 均视为未就绪
        return False, str(e)[:120]


def container_name(cs) -> str:
    return f"ft-cache-{cs.id}"


def _task_key(cs) -> str:
    return f"cache-{cs.id}"


# ---------- 启动令牌（作废旧后台线程，与部署 start/stop 同机制） ----------

_cache_tokens: dict[int, int] = {}
_cache_token_lock = threading.Lock()


def _bump_cache_token(cs_id: int) -> int:
    with _cache_token_lock:
        _cache_tokens[cs_id] = _cache_tokens.get(cs_id, 0) + 1
        return _cache_tokens[cs_id]


def _cache_token_stale(cs_id: int, tok: int) -> bool:
    with _cache_token_lock:
        return _cache_tokens.get(cs_id, 0) != tok


def _log_dir(cs) -> str:
    return os.path.join(settings.data_dir, "infer", "cache", str(cs.id))


def start_container(cs) -> str:
    """创建并启动缓存容器（同步，仅 docker API），返回容器 id。
    规格由 render_spec 渲染（LMCache 编排型按 config，旧路径用顶层字段）。
    command 非空 = pause 占位 + exec（日志落盘、退出码可查）；空 = 镜像默认入口直跑。"""
    name = container_name(cs)
    tc.remove_container(name, force=True)
    spec = render_spec(cs)
    cid = tc.create_container(
        name=name, image=spec["image"], gpus=0, cpus=4, mem_gb=spec["mem_gb"],
        env=spec["env"], labels={"fintune.type": "cache", "fintune.cache": str(cs.id)},
        use_image_entrypoint=not bool(spec["command"]),
    )
    if spec["command"]:
        tc.start_task(_task_key(cs), cid, ["bash", "-lc", spec["command"] + " 2>&1"],
                      os.path.join(_log_dir(cs), "run.log"))
    return cid


def stop_container(cs) -> None:
    """停止并删除容器；换发令牌作废旧启动线程（防镜像拉取完成后"复活"）。"""
    _bump_cache_token(cs.id)
    tc.remove_container(container_name(cs), force=True)


def _probe_tcp(ip: str, port: int, timeout: float = 3.0) -> bool:
    try:
        with socket.create_connection((ip, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def _process_exit(cs) -> tuple[bool, int | None]:
    """（进程是否已退出, 退出码）；渲染后 command 为空的容器主进程即服务本身，恒 (False, None)"""
    if not render_spec(cs)["command"]:
        return False, None
    st = tc.task_state(_task_key(cs))
    if st.get("known") and st.get("done"):
        return True, st.get("code")
    return False, None


def _health_of(cs, name: str) -> tuple[bool, str]:
    """(healthy, 说明)：容器存活 + 启动进程未异常退出 + 端口就绪。
    LMCache 编排型走 HTTP /healthcheck（metrics 端口，强于 TCP）；旧路径走 TCP。"""
    if not tc.container_alive(name):
        return False, "容器不存在或未运行"
    done, code = _process_exit(cs)
    if done:
        return False, f"启动进程已退出（code={code}）"
    spec = render_spec(cs)
    ip = tc.container_ip(name)
    if spec["metrics_port"]:
        if not ip:
            return False, "容器 IP 未获取到"
        ok, msg = _health_http(ip, spec["metrics_port"])
        return ok, (msg or f"健康检查端口 {spec['metrics_port']} 未就绪")
    if spec["port"]:
        if ip and not _probe_tcp(ip, spec["port"]):
            return False, f"服务端口 {spec['port']} 未就绪"
    return True, ""


# 端口探活抖动抑制：连续 3 次不健康才降级 failed（与部署指标探活同口径）
_unhealthy_ticks: dict[int, int] = {}


def reconcile(db) -> None:
    """调度器 tick 收敛：creating→running（健康）；running→stopped/failed（进程退出）；
    端口抖动 3 连败降级 failed；容器重建后 IP 漂移自动刷新 endpoint。"""
    for cs in db.query(CacheService).all():
        name = container_name(cs)
        if cs.status not in ("creating", "running"):
            continue
        spec = render_spec(cs)
        if not spec["image"]:
            # 逻辑型缓存（无镜像）不应当处于容器态；兜底收敛为 stopped
            cs.status = "stopped"
            cs.state_message = ""
            cs.endpoint = ""
            cs.healthy = False
            continue
        healthy, msg = _health_of(cs, name)
        cs.healthy = healthy
        cs.last_check_at = now()
        alive = tc.container_alive(name)
        ip = tc.container_ip(name) if alive else ""
        if healthy:
            _unhealthy_ticks.pop(cs.id, None)
            if cs.status == "creating":
                cs.status = "running"
                cs.state_message = ""
            if ip and spec["port"]:
                ep = f"{ip}:{spec['port']}"
                if cs.endpoint != ep:
                    cs.endpoint = ep
            continue
        # ---- 不健康 ----
        done, code = _process_exit(cs)
        if alive and not done:
            # 容器在、进程在、仅端口探活失败 → 抖动抑制，不立即降级
            n = _unhealthy_ticks.get(cs.id, 0) + 1
            _unhealthy_ticks[cs.id] = n
            if n >= 3 and cs.status == "running":
                cs.status = "failed"
                cs.state_message = f"连续 {n} 次健康检查失败：{msg}"
                cs.endpoint = ""
            elif cs.status == "running":
                cs.state_message = msg  # 展示提示，保持 running
            # creating 阶段保持等待（进程启动中 / 端口未监听）
            continue
        # 进程已退出 或 容器消失
        if cs.status == "creating" and not alive and not done:
            # 启动线程可能仍在等镜像：镜像明确失败才由 reconcile 兜底置 failed
            ist = image_ops.ensure_image(spec["image"])
            if ist == "failed":
                cs.status = "failed"
                cs.state_message = f"镜像下载失败：{image_ops.image_status(spec['image']).get('error', '')[:200]}"
            continue
        failed = done and not (isinstance(code, int) and code == 0)
        cs.status = "failed" if failed else "stopped"
        cs.state_message = msg
        cs.endpoint = ""
    db.commit()


def _write_status(db, cs_id: int, status: str, msg: str, cid: str | None = None) -> None:
    cs = db.get(CacheService, cs_id)
    if not cs:
        return
    cs.status = status
    cs.state_message = msg
    if cid is not None:
        cs.container_id = cid
    if status in ("stopped", "failed"):
        cs.endpoint = ""
    db.commit()


def launch_cache_thread(cs_id: int, note: str = "") -> None:
    """后台启动：镜像就绪等待（>1h 超时）→ 建容器 → 交 reconcile 收敛健康态。
    令牌机制：stop/delete 换发令牌后，本线程在镜像就绪后不再建容器。"""
    db0 = SessionLocal()
    try:
        cs = db0.get(CacheService, cs_id)
        if not cs:
            return
        cs.status = "creating"
        cs.state_message = note or "镜像准备中…"
        cs.endpoint = ""
        cs.spec_digest = spec_digest_of(cs)
        db0.commit()
        image = render_spec(cs)["image"]
    finally:
        db0.close()
    tok = _bump_cache_token(cs_id)

    def _run():
        db = SessionLocal()
        try:
            deadline = time.time() + 3600
            while True:
                if _cache_token_stale(cs_id, tok):
                    return
                ist = image_ops.ensure_image(image)
                if ist == "ready":
                    break
                if ist == "failed":
                    _write_status(db, cs_id, "failed",
                                  f"镜像下载失败：{image_ops.image_status(image).get('error', '')[:400]}")
                    return
                if time.time() > deadline:
                    _write_status(db, cs_id, "failed", "镜像下载超时（>1h），请检查网络后重试")
                    return
                time.sleep(10)
            if _cache_token_stale(cs_id, tok):
                return
            cs = db.get(CacheService, cs_id)
            if not cs:
                return
            cid = start_container(cs)
            _write_status(db, cs_id, "creating", "启动中，等待服务就绪…", cid=cid)
        except Exception as e:
            log.exception("cache service %s launch failed", cs_id)
            _write_status(db, cs_id, "failed", str(e)[:400])
        finally:
            db.close()

    threading.Thread(target=_run, name=f"cache-launch-{cs_id}", daemon=True).start()


def _route_cache_id(db, dep_id: int) -> int | None:
    """部署被启用的路由引用且路由关联缓存 → 路由级缓存 id（优先于部署级）。
    多路由同挂一部署时取最小 id 路由（确定性）。"""
    from ..models import ModelRoute, ModelRouteTarget
    tgt = db.query(ModelRouteTarget).filter_by(deployment_id=dep_id) \
        .order_by(ModelRouteTarget.id).first()
    if not tgt:
        return None
    r = db.get(ModelRoute, tgt.route_id)
    if r and r.enabled and r.cache_service_id:
        return r.cache_service_id
    return None


def cache_env_for(dep) -> dict:
    """缓存端点 → VD_CACHE_ENDPOINT 注入（http://ip:port）。
    解析优先级：路由级缓存（部署所属启用路由的关联）> 部署级关联。
    未关联 / 服务未运行 / 无地址 → 空 dict（部署不受影响，优雅降级）。"""
    cid = getattr(dep, "cache_service_id", None)
    db = SessionLocal()
    try:
        rid = _route_cache_id(db, dep.id)
        if rid:
            cid = rid
        if not cid:
            return {}
        cs = db.get(CacheService, cid)
        if cs and cs.status == "running" and cs.endpoint:
            return {"VD_CACHE_ENDPOINT": f"http://{cs.endpoint}"}
        return {}
    finally:
        db.close()

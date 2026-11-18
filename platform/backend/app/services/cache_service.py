"""缓存加速服务实例（托管容器，独立于推理部署）：生命周期 + 状态收敛 + 部署端点注入

容器形态与推理部署一致（pause 占位 + 容器内 exec 启动命令）；
command 留空 = 按镜像默认入口直跑（现成 KV 缓存存储镜像）。
状态机：stopped → creating（镜像准备→容器启动）→ running（健康检查通过）
        → stopped（优雅退出/人工停止）/ failed（进程非零退出，需人工 start）。
部署关联（Deployment.cache_service_id）后向其实例注入 VD_CACHE_ENDPOINT=http://ip:port。
"""
import logging
import os
import socket
import threading
import time

from ..config import settings
from ..database import SessionLocal
from ..models import CacheService
from . import taskcontainer as tc
from . import image_ops

log = logging.getLogger("cache-service")


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
    command 非空 = pause 占位 + exec（日志落盘、退出码可查）；
    空 = 镜像默认入口直跑。"""
    name = container_name(cs)
    tc.remove_container(name, force=True)
    env = {str(k): str(v) for k, v in (cs.env or {}).items()}
    cmd = (cs.command or "").strip()
    cid = tc.create_container(
        name=name, image=cs.image, gpus=0, cpus=4, mem_gb=8,
        env=env, labels={"fintune.type": "cache", "fintune.cache": str(cs.id)},
        use_image_entrypoint=not bool(cmd),
    )
    if cmd:
        tc.start_task(_task_key(cs), cid, ["bash", "-lc", cmd + " 2>&1"],
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
    """（进程是否已退出, 退出码）；command 留空的容器主进程即服务本身，恒 (False, None)"""
    if not (cs.command or "").strip():
        return False, None
    st = tc.task_state(_task_key(cs))
    if st.get("known") and st.get("done"):
        return True, st.get("code")
    return False, None


def _health_of(cs, name: str) -> tuple[bool, str]:
    """(healthy, 说明)：容器存活 + 启动进程未异常退出 + 声明端口 TCP 可达"""
    if not tc.container_alive(name):
        return False, "容器不存在或未运行"
    done, code = _process_exit(cs)
    if done:
        return False, f"启动进程已退出（code={code}）"
    if cs.port:
        ip = tc.container_ip(name)
        if ip and not _probe_tcp(ip, cs.port):
            return False, f"服务端口 {cs.port} 未就绪"
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
        healthy, msg = _health_of(cs, name)
        alive = tc.container_alive(name)
        ip = tc.container_ip(name) if alive else ""
        if healthy:
            _unhealthy_ticks.pop(cs.id, None)
            if cs.status == "creating":
                cs.status = "running"
                cs.state_message = ""
            if ip and cs.port:
                ep = f"{ip}:{cs.port}"
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
            ist = image_ops.ensure_image(cs.image)
            if ist == "failed":
                cs.status = "failed"
                cs.state_message = f"镜像下载失败：{image_ops.image_status(cs.image).get('error', '')[:200]}"
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
        db0.commit()
        image = cs.image
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


def cache_env_for(dep) -> dict:
    """部署关联的缓存服务 → VD_CACHE_ENDPOINT 注入（http://ip:port）。
    未关联 / 服务未运行 / 无地址 → 空 dict（部署不受影响，优雅降级）。"""
    cid = getattr(dep, "cache_service_id", None)
    if not cid:
        return {}
    db = SessionLocal()
    try:
        cs = db.get(CacheService, cid)
        if cs and cs.status == "running" and cs.endpoint:
            return {"VD_CACHE_ENDPOINT": f"http://{cs.endpoint}"}
        return {}
    finally:
        db.close()

"""任务容器服务：每个任务独立容器 + pause 占位 + docker exec 通信

设计要点（按需求）：
- 每个任务（训练/评测/推理/数据）都新起一个独立容器，资源彼此隔离。
- 容器主进程是 pause 占位（sleep infinity），保证容器常驻、可被其他服务
  通过 docker exec 通信（启动真实任务进程、tail 日志、跑 nvidia-smi 等）。
- 每任务独立绑定 GPU 设备、内存/CPU 上限，实现细粒度资源隔离。
- 真实任务进程通过 exec 在占位容器内拉起，stdout 落盘为 run.log，
  完成后记录退出码，供调度器轮询收敛任务状态。
"""
import os
import time
import threading
import logging
import docker
from docker import errors as docker_errors

from ..config import settings

log = logging.getLogger("taskcontainer")

_client = None


def docker_client():
    global _client
    if _client is None:
        _client = docker.from_env()
    return _client


# 运行中的 exec 任务注册表: task_key -> {"done":bool,"code":int,"thread":Thread}
RUNNERS: dict[str, dict] = {}
_lock = threading.Lock()


def _volumes(base: dict):
    """统一挂载 /data 到容器内 /data"""
    vols = {f"{settings.data_dir}": {"bind": "/data", "mode": "rw"}}
    vols.update(base or {})
    return vols


def create_container(name: str, image: str, gpus: int = 0, cpus: float | None = None,
                     mem_gb: float | None = None, env: dict | None = None,
                     volumes: dict | None = None, labels: dict | None = None) -> str:
    """创建 pause 占位容器（主进程 sleep infinity），返回容器 id。"""
    client = docker_client()
    kwargs = dict(
        image=image,
        name=name,
        command=["sleep", "infinity"],  # pause 占位主进程
        detach=True,
        auto_remove=False,
        network=settings.docker_network,
        environment={
            "HF_HOME": "/data/hf-cache",
            "HF_ENDPOINT": settings.hf_endpoint,
            "WANDB_MODE": "disabled",
            "PYTHONUNBUFFERED": "1",
            **(env or {}),
        },
        volumes=_volumes(volumes),
        labels={**(labels or {}), "fintune.owner": "backend", "fintune.created": str(int(time.time()))},
        stdout=False, stderr=False,
    )
    if gpus and gpus > 0:
        # docker SDK 7.x: GPUDevice 已移除，用 DeviceRequest + nvidia-container-toolkit 钩子
        kwargs["device_requests"] = [
            docker.types.DeviceRequest(count=gpus, capabilities=[["gpu"]])
        ]
    if cpus:
        # Docker 29 + docker-py 7.x: 用 cpu_quota/cpu_period 设置 CPU 限额
        # （实测 Docker 29 daemon 将 nano_cpus 误限流为固定 0.01 核，quota/period 行为正常）
        kwargs["cpu_quota"] = int(cpus * 100000)
        kwargs["cpu_period"] = 100000
    if mem_gb:
        kwargs["mem_limit"] = f"{int(mem_gb * 1024 ** 3)}"
    ctr = client.containers.run(**kwargs)
    log.info("created placeholder container %s (id=%s, gpus=%s)", name, ctr.id[:12], gpus)
    return ctr.id


def exec_run(name_or_id: str, cmd, timeout: int = 300) -> tuple[int, str]:
    """在占位容器内同步执行一条命令（短任务/探测），返回 (exit_code, output)。"""
    client = docker_client()
    exec_id = client.api.exec_create(name_or_id, cmd, stdout=True, stderr=True)
    out = client.api.exec_start(exec_id, detach=False)
    info = client.api.exec_inspect(exec_id)
    text = out.decode("utf-8", "ignore") if isinstance(out, (bytes, bytearray)) else str(out)
    return info.get("ExitCode", 0), text


def start_task(task_key: str, name_or_id: str, cmd, log_path: str) -> None:
    """在占位容器内后台 exec 拉起真实任务进程，stdout 追加落盘，完成后记录退出码。"""
    client = docker_client()
    with _lock:
        if task_key in RUNNERS and not RUNNERS[task_key].get("done", False):
            log.warning("task %s already running", task_key)
            return

    def _run():
        code = -1
        try:
            exec_id = client.api.exec_create(name_or_id, cmd, stdout=True, stderr=True, tty=False)
            os.makedirs(os.path.dirname(log_path), exist_ok=True)
            with open(log_path, "ab") as lf:
                stream = client.api.exec_start(exec_id, detach=False, stream=True)
                for chunk in stream:
                    if chunk:
                        lf.write(chunk if isinstance(chunk, (bytes, bytearray)) else chunk.encode())
                        lf.flush()
            info = client.api.exec_inspect(exec_id)
            code = info.get("ExitCode", -1)
        except Exception as e:
            log.exception("exec task %s failed", task_key)
            try:
                with open(log_path, "ab") as lf:
                    lf.write(f"\n[taskcontainer] exec error: {e}\n".encode())
            except Exception:
                pass
        with _lock:
            RUNNERS[task_key]["done"] = True
            RUNNERS[task_key]["code"] = code
        log.info("task %s finished code=%s", task_key, code)

    with _lock:
        RUNNERS[task_key] = {"done": False, "code": None, "thread": None}
    t = threading.Thread(target=_run, args=(), name=f"task-{task_key}", daemon=True)
    t.start()
    with _lock:
        RUNNERS[task_key]["thread"] = t


def task_state(task_key: str) -> dict:
    with _lock:
        r = RUNNERS.get(task_key)
        if not r:
            return {"known": False, "done": False, "code": None}
        return {"known": True, "done": r.get("done", False), "code": r.get("code")}


def container_ip(name_or_id: str) -> str:
    client = docker_client()
    try:
        ctr = client.containers.get(name_or_id)
        nets = ctr.attrs.get("NetworkSettings", {}).get("Networks", {})
        for net in nets.values():
            if net.get("IPAddress"):
                return net["IPAddress"]
    except docker_errors.NotFound:
        pass
    return ""


def container_alive(name_or_id: str) -> bool:
    client = docker_client()
    try:
        return client.containers.get(name_or_id).status == "running"
    except docker_errors.NotFound:
        return False


def container_logs(name_or_id: str, tail: int = 200) -> str:
    client = docker_client()
    try:
        return client.containers.get(name_or_id).logs(tail=tail).decode("utf-8", "ignore")
    except docker_errors.NotFound:
        return ""


def remove_container(name_or_id: str, force: bool = True) -> None:
    client = docker_client()
    try:
        ctr = client.containers.get(name_or_id)
        ctr.remove(force=force)
        log.info("removed container %s", name_or_id)
    except docker_errors.NotFound:
        pass


def pull_image(image: str) -> bool:
    client = docker_client()
    try:
        client.images.pull(image)
        return True
    except Exception as e:
        log.error("pull %s failed: %s", image, e)
        return False

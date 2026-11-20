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
import re
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


def _storage_extra_paths() -> dict:
    """已注册 path 型存储后端根目录 → 同路径 bind（worker 容器可直接访问选在这些
    后端上的数据集/模型文件）。

    注意：/data 内的挂载点（如 JuiceFS 的 /data/juicefs FUSE 子挂载）**必须**显式
    同路径 bind——容器 /data 的默认 rprivate bind 不传播宿主子挂载（已实测：宿主
    /data 下的 bind/FUSE 子挂载在容器内是空目录）。仅挂宿主上已存在的路径。"""
    vols = {}
    try:
        from ..models import StorageBackend
        from ..database import SessionLocal
        from . import storage
        db = SessionLocal()
        try:
            rows = db.query(StorageBackend).filter(
                StorageBackend.type.in_(storage.PATH_BASED_TYPES)).all()
            for b in rows:
                p = storage.backend_root(b)
                if not p or not os.path.exists(p):
                    continue
                rp = os.path.realpath(p)
                vols[rp] = {"bind": rp, "mode": "rw"}
        finally:
            db.close()
    except Exception as e:
        log.warning("storage extra paths: %s", e)
    return vols


def _volumes(base: dict):
    """统一挂载 /data 到容器内 /data（+ 存储后端根同路径 bind，覆盖 FUSE 子挂载）"""
    vols = {f"{settings.data_dir}": {"bind": "/data", "mode": "rw"}}
    vols.update(_storage_extra_paths())
    vols.update(base or {})
    return vols


def _rdma_devices() -> list[dict]:
    """宿主 IB/RDMA 字符设备（uverbsN + rdma_cm）；无 RDMA 网卡时返回 []。
    注入任务容器后 NCCL 可自动发现 HCA 走 IB 通信（多机训练 / PD 分离 KV 传输）。

    backend 自身是容器（见不到宿主 /dev）：优先读自己挂载的 /dev/infiniband
    （compose 已 ro 挂载），否则回退 gpu-agent 从宿主采集并写入节点 meta 的
    ib_devices 列表（按名字构造宿主路径）。两路都空 = 宿主无 RDMA。"""
    import glob
    names = sorted(os.path.basename(p) for p in glob.glob("/dev/infiniband/uverbs*"))
    if not names:
        names = _rdma_names_from_meta()
    out = []
    for n in names:
        host = f"/dev/infiniband/{n}"
        out.append({"PathOnHost": host, "PathInContainer": host, "CgroupPermissions": "rwm"})
    if os.path.exists("/dev/infiniband/rdma_cm") or "rdma_cm" in _rdma_names_from_meta():
        out.append({"PathOnHost": "/dev/infiniband/rdma_cm",
                    "PathInContainer": "/dev/infiniband/rdma_cm", "CgroupPermissions": "rwm"})
    return out


def _rdma_names_from_meta() -> list[str]:
    """本节点 meta 登记的 IB 设备名（gpu-agent 在宿主侧采集；逗号分隔）"""
    try:
        from ..models import GpuNode
        from ..database import SessionLocal
        db = SessionLocal()
        try:
            n = db.query(GpuNode).filter_by(name=settings.node_name).first()
            raw = (n.meta or {}).get("ib_devices", "") if n else ""
            return [x.strip() for x in str(raw).split(",") if x.strip()]
        finally:
            db.close()
    except Exception:
        return []


def rdma_info() -> dict:
    """宿主 RDMA/IB 状态（UI 展示 + 部署表单提示）：{present, devices:[uverbs0,...]}"""
    import glob
    devs = sorted(os.path.basename(p) for p in glob.glob("/dev/infiniband/uverbs*"))
    if not devs:
        devs = [d for d in _rdma_names_from_meta() if d.startswith("uverbs")]
    return {"present": bool(devs), "devices": devs}


# ---------- GPU 显式分配（多实例/多副本防撞卡） ----------
# nvidia-container-toolkit 的 `--gpus N` 按索引取前 N 张卡、不避让已占用卡：
# 两个 TP1 容器会同拿 GPU0 → OOM。平台在创建容器前显式分配物理卡
# （DeviceIDs + 容器标签预留），引擎看到的仍是 0..N-1 重编号。

class GpuNotEnough(Exception):
    """空闲 GPU 不足（请求数 > 宿主空闲卡 − 平台已预留）"""


_gpu_alloc_lock = threading.Lock()
_GPU_BUSY_MEM_MIB = 2048  # 显存占用超此值视为被外部进程占用（任务容器未起引擎前仅 ~1MiB）


def _host_gpu_mem_used() -> dict[int, float] | None:
    """各卡当前显存占用 {index: MiB}（DCGM → Prometheus）；不可用返回 None"""
    try:
        from . import prom_service
        return prom_service.gpu_mem_used_map()
    except Exception:
        return None


def _fintune_reserved_gpus() -> set[int]:
    """平台任务容器预留的卡（容器标签 fintune.gpus）。

    计入规则（兼顾"防并发双占"与"防残留锁卡"）：
      - running 容器：一定在占卡，计入。
      - created 容器：assign→create→start 的窗口内容器还是 created（几秒内
        转 running）。计入**最近创建（<120s）**的 created 容器以堵住并发双占；
        更久的 created = 失败/中止部署的残留（从未真正启动，不占显存），不计入——
        否则残留容器会永久锁死 GPU（实测：失败部署留下 2 个 created 容器占满
        2 卡，后续部署全部报"空闲 GPU 不足"）。
      - exited/stopped/paused：已不占卡，不计入。"""
    try:
        client = docker_client()
        now = time.time()
        reserved: set[int] = set()
        for ctr in client.containers.list(all=True, filters={"label": "fintune.owner=backend"}):
            status = (getattr(ctr, "status", "") or "").lower()
            fresh = False
            if status == "running":
                fresh = True
            elif status == "created":
                created_ts = None
                lab = (ctr.labels or {}).get("fintune.created", "")
                if str(lab).strip().isdigit():
                    created_ts = float(lab)
                if created_ts is None:
                    try:
                        created_ts = ctr.attrs["Created"][:19].replace("T", " ")
                        created_ts = time.mktime(time.strptime(created_ts, "%Y-%m-%d %H:%M:%S"))
                    except Exception:
                        created_ts = None
                fresh = created_ts is not None and (now - created_ts) < 120
            else:
                fresh = False
            if not fresh:
                continue
            for part in str((ctr.labels or {}).get("fintune.gpus", "")).split(","):
                if part.strip().isdigit():
                    reserved.add(int(part.strip()))
        return reserved
    except Exception:
        return set()


def _local_gpu_count() -> int:
    """本节点登记卡数（GpuNode 表）；未知返回 0"""
    try:
        from ..models import GpuNode
        from ..database import SessionLocal
        db = SessionLocal()
        try:
            n = db.query(GpuNode).filter_by(name=settings.node_name).first()
            return int(n.gpu_count or 0) if n else 0
        finally:
            db.close()
    except Exception:
        return 0


def _gpu_free_snapshot() -> tuple[list[int], set[int], int, dict | None]:
    """单次「空闲 GPU」快照：(free, reserved, total, mem)。

    空闲 = 未被平台容器预留 且 DCGM 显存低于阈值（无外部进程）。
    枚举不到卡（DCGM 未就绪/节点未登记）时 total=0，由调用方退回旧行为。"""
    mem = _host_gpu_mem_used()
    reserved = _fintune_reserved_gpus()
    total = max(_local_gpu_count(), len(mem) if mem else 0)
    if total == 0:
        return [], set(), 0, mem
    free = [i for i in range(total)
            if i not in reserved and (mem is None or mem.get(i, 0) < _GPU_BUSY_MEM_MIB)]
    return free, reserved, total, mem


def assign_gpus(n: int) -> list[int]:
    """分配 n 张空闲 GPU 卡，返回物理索引（升序）。

    空闲 = 宿主显存低于阈值（无外部进程）且未被平台容器预留。
    DCGM→Prometheus 存在最长约 45s 滞后（dcgm 更新 30s + scrape 15s）：平台刚释放
    的卡仍被报高占用，直接判会误报"外部占用"。故先在**锁外**等指标沉降（让刚释放
    的卡从"外部占用"翻转为空闲）——真正被外部占用的卡整段等待都保持占用，等待后
    仍不足则正确报错；最后**锁内**做权威「检查+绑定」（等待期状态可能变化），
    串行化并发分配防双占。常见情况（卡充足）首次即命中，无任何等待。"""
    free, reserved, total, mem = _gpu_free_snapshot()
    if total > 0 and len(free) < n:
        for _ in range(9):  # 最多 ~45s，覆盖 DCGM+scrape 滞后窗口
            time.sleep(5)
            free, reserved, total, mem = _gpu_free_snapshot()
            if total == 0 or len(free) >= n:
                break
    with _gpu_alloc_lock:
        free, reserved, total, mem = _gpu_free_snapshot()  # 锁内权威复检
        if total == 0:
            return list(range(n))  # 枚举不到卡退回旧行为，由引擎真实撞卡时报错
        if len(free) < n:
            ext = [i for i in range(total) if i not in reserved
                   and mem is not None and mem.get(i, 0) >= _GPU_BUSY_MEM_MIB]
            raise GpuNotEnough(
                f"空闲 GPU 不足：需 {n} 张，可用 {len(free)} 张"
                f"（宿主共 {total} 卡：外部占用 {len(ext)}、平台预留 {len(reserved & set(range(total)))}）")
        return free[:n]


def _parse_size(s: str) -> int:
    """docker 尺寸字符串 → 字节（支持 b/k/m/g 后缀，纯数字=字节）"""
    s = str(s).strip().lower()
    m = re.fullmatch(r"(\d+(?:\.\d+)?)([bkmgt]?)", s)
    if not m:
        raise ValueError(f"非法尺寸: {s}")
    n = float(m.group(1))
    mult = {"": 1, "b": 1, "k": 1024, "m": 1024 ** 2,
            "g": 1024 ** 3, "t": 1024 ** 4}[m.group(2)]
    return int(n * mult)


# docker run 参数白名单 → docker-py kwargs（部署「Docker 参数」逐行解析；
# 只放行安全可控的运行参数，未知 flag 拒绝——避免把任意 daemon 参数面暴露给前端）
def parse_docker_args(args) -> dict:
    """解析 docker 参数列表为 docker-py kwargs。未知参数/非法值抛 ValueError。

    支持：--ipc=host|private|shareable / --shm-size=16g / --cap-add=X（可多次）
          --cap-drop=X（可多次）/ --pids-limit=N / --security-opt=X（可多次）
    与内置行为的关系：用户显式参数最后应用（可覆盖 RDMA 自动注入的 ipc=host 等）。"""
    out: dict = {}
    for raw in args or []:
        s = str(raw).strip()
        if not s:
            continue
        if not s.startswith("--"):
            raise ValueError(f"不支持的 Docker 参数: {s}（须为 --flag=value 形式）")
        flag, _, val = s[2:].partition("=")
        flag, val = flag.lower(), val.strip()
        if flag == "ipc":
            if val not in ("host", "private", "shareable"):
                raise ValueError(f"不支持的 --ipc 取值: {val}（host/private/shareable）")
            out["ipc_mode"] = val
        elif flag == "shm-size":
            out["shm_size"] = _parse_size(val)
        elif flag in ("cap-add", "cap-drop"):
            if not val:
                raise ValueError(f"--{flag} 需要能力名（如 SYS_PTRACE）")
            out.setdefault("cap_add" if flag == "cap-add" else "cap_drop", []).append(val.upper())
        elif flag == "pids-limit":
            out["pids_limit"] = int(val)
        elif flag == "security-opt":
            if not val:
                raise ValueError("--security-opt 需要取值（如 apparmor=unconfined）")
            out.setdefault("security_opt", []).append(val)
        else:
            raise ValueError(
                f"不支持的 Docker 参数: --{flag}"
                f"（支持: ipc / shm-size / cap-add / cap-drop / pids-limit / security-opt）")
    return out


def create_container(name: str, image: str, gpus: int = 0, cpus: float | None = None,
                     mem_gb: float | None = None, env: dict | None = None,
                     volumes: dict | None = None, labels: dict | None = None,
                     gpu_ids: list[int] | None = None, use_image_entrypoint: bool = False,
                     extra_kwargs: dict | None = None) -> str:
    """创建 pause 占位容器（主进程 sleep infinity），返回容器 id。

    镜像懒加载：本地缺失时自动后台拉取/构建并抛 ImageNotReady，
    由调度器/启动线程轮询 ensure_image 就绪后再重试（部署时不预下载）。

    gpu_ids 为 None 且 gpus>0 时自动分配空闲物理卡（assign_gpus），
    并以 DeviceIDs 显式绑定 + 标签 fintune.gpus 预留（容器删除即释放）。

    use_image_entrypoint=True 时不覆盖 ENTRYPOINT，直接按镜像默认入口运行
    （缓存加速服务等"无自定义启动命令、用现成服务镜像"的场景）；
    返回后容器已在跑（而非 sleep 占位等待 exec）。

    extra_kwargs：docker-py run 额外 kwargs（部署「Docker 参数」经
    parse_docker_args 白名单解析而来；最后应用，用户显式参数优先于内置行为）。
    """
    from . import image_ops
    st = image_ops.ensure_image(image)
    if st != "ready":
        extra = f": {image_ops.image_status(image).get('error', '')}" if st == "failed" else ""
        raise image_ops.ImageNotReady(f"镜像 {image} 未就绪（{st}）{extra}")
    client = docker_client()
    # 显式 GPU 分配：默认自动挑空闲物理卡（防多容器撞同一张卡）；调用方可指定
    if gpus and gpus > 0:
        if gpu_ids is None:
            gpu_ids = assign_gpus(gpus)
        gpu_ids = [int(i) for i in (gpu_ids or [])][:gpus] or list(range(gpus))
    labels = {**(labels or {}), "fintune.owner": "backend", "fintune.created": str(int(time.time()))}
    if gpu_ids:
        labels["fintune.gpus"] = ",".join(str(i) for i in gpu_ids)
    kwargs = dict(
        image=image,
        name=name,
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
        labels=labels,
        stdout=False, stderr=False,
    )
    if not use_image_entrypoint:
        # pause 占位主进程：显式覆盖镜像 ENTRYPOINT（官方镜像如 vllm/vllm-openai
        # 的 ENTRYPOINT 是 vllm 本身，不覆盖会执行 "vllm sleep infinity" 直接崩掉）
        kwargs["entrypoint"] = ["sleep", "infinity"]
    if gpus and gpus > 0:
        # docker SDK 7.x: GPUDevice 已移除，用 DeviceRequest + nvidia-container-toolkit 钩子。
        # 显式 device_ids 绑定物理卡（容器内引擎看到 0..N-1 重编号）。
        # 注意：Docker daemon 禁止 DeviceRequest 同时设 Count 与 DeviceIDs（500:
        # "cannot set both Count and DeviceIDs"），显式绑卡时只传 device_ids、count 留空。
        kwargs["device_requests"] = [
            docker.types.DeviceRequest(device_ids=[str(i) for i in gpu_ids],
                                       capabilities=[["gpu"]])
        ]
        # NCCL SHM 传输：Docker 默认 /dev/shm 仅 64MB，多卡/TP 训练与推理在
        # NCCL 初始化时即报 "Error while creating shared memory segment
        # /dev/shm/nccl-*"（2026-11-18 双卡 ZeRO-2 e2e 实测）。显式 16GB——
        # tmpfs 预留不等于实际占用（按真实分配计费），单卡容器同样受益
        # （多机单卡的 NCCL 本地 staging 也走 SHM）。
        kwargs["shm_size"] = 16 * 1024 ** 3
    rdma = _rdma_devices()
    if rdma:
        # RDMA/IB：注入 HCA 字符设备；ipc=host 供 NCCL 共享内存；
        # 宿主 libibverbs 只读挂入（引擎镜像默认无 IB 用户态库，缺则 NCCL 发现不了 HCA；
        # backend 容器见不到宿主 /usr/lib，故按候选路径直接挂——仅宿主真有 IB 才进此分支）
        kwargs["devices"] = rdma
        kwargs["ipc_mode"] = "host"
        for host_lib in ("/usr/lib/x86_64-linux-gnu/libibverbs", "/usr/lib64/libibverbs", "/etc/libibverbs"):
            kwargs["volumes"][host_lib] = {"bind": host_lib, "mode": "ro"}
        kwargs["environment"]["NCCL_IB_ENABLE"] = "1"
        log.info("container %s: RDMA devices injected (%s)", name, ", ".join(d["PathOnHost"] for d in rdma))
    if cpus:
        # Docker 29 + docker-py 7.x: 用 cpu_quota/cpu_period 设置 CPU 限额
        # （实测 Docker 29 daemon 将 nano_cpus 误限流为固定 0.01 核，quota/period 行为正常）
        kwargs["cpu_quota"] = int(cpus * 100000)
        kwargs["cpu_period"] = 100000
    if mem_gb:
        kwargs["mem_limit"] = f"{int(mem_gb * 1024 ** 3)}"
    # 部署级 Docker 参数（白名单解析后的 kwargs；用户显式参数覆盖内置默认）
    if extra_kwargs:
        kwargs.update(extra_kwargs)
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


def start_task(task_key: str, name_or_id: str, cmd, log_path: str, env: dict | None = None) -> None:
    """在占位容器内后台 exec 拉起真实任务进程，stdout 追加落盘，完成后记录退出码。

    env：exec 级环境变量（区别于 create_container 的容器级 env）；
    用于依赖容器创建后才知的值（如 NIXL side-channel 需本容器 IP）。"""
    client = docker_client()
    with _lock:
        if task_key in RUNNERS and not RUNNERS[task_key].get("done", False):
            log.warning("task %s already running", task_key)
            return

    def _run():
        code = -1
        try:
            exec_id = client.api.exec_create(name_or_id, cmd, stdout=True, stderr=True,
                                             tty=False, environment=env or None)
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

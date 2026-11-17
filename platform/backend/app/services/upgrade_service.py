"""集群升级服务：引擎镜像拉取 / 运行时 pin 覆盖 / 组件容器蓝绿重建

组件与升级语义：
- vllm / sglang / llamafactory：官方镜像，docker pull 目标 tag → 写运行时 pin
  （/data/upgrade/image_pins.json），新任务/部署使用新镜像，运行中的不受影响
- gpu_agent：DCGM Agent（nvidia/dcgm），pull + 蓝绿重建容器（监控中断数秒）
- evalscope：评测引擎官方开源 ModelScope 镜像（ACR，内置 evalscope/openai/
  jieba/rouge-chinese），pull 目标 tag → 写运行时 pin；平台 runner.py 由 backend
  运行时投递进容器（非 compose 服务），仅新评测任务生效（运行中评测容器不受影响）；
  离线/气隔环境可把 IMAGES_EVAL 指回 fintune/evalscope:1.0 走 /repo 自构兜底
- mineru：文档解析常驻容器（fintune/mineru:1.0），重建同 tag + 蓝绿重建
  容器（解析服务中断数秒）
- backend / web：自构镜像（fintune/*:1.0），以宿主源码树 /repo 重建同 tag +
  蓝绿重建容器（平台短暂不可用约 1 分钟；backend 自升级时 worker 线程随旧容器
  消亡，新进程调度器每轮调 converge() 完成任务收敛）

容器蓝绿重建：从旧容器 attrs 推导完整 docker-py 参数（env/volumes/网络/GPU/
重启策略），先建临时名新容器（服务别名保持 DNS 解析），验证 running 后停旧
删旧改名，接近零停机。
"""
import json
import logging
import os
import re
import threading
import time
from datetime import datetime

from ..config import settings
from ..models import UpgradeTask

log = logging.getLogger("upgrade_service")

# GPU Agent 基础镜像（compose pin；可被运行时 pin 覆盖）
GPU_AGENT_IMAGE = "nvidia/dcgm:4.7.1-ubuntu24.04"

HUB = "https://hub.docker.com/v2/repositories"
_LATEST_TTL = 600  # 最新版本检查缓存 10 分钟

# key -> 组件定义
#   kind: pull（官方镜像换 tag）| build（自构镜像以 /repo 重建同 tag）
#   impact: engine（仅新任务生效）| agent（重建 agent 容器）| self（重建自身容器）
COMPONENTS: dict[str, dict] = {
    "vllm": {"label": "vLLM 推理引擎", "kind": "pull", "hub": "vllm/vllm-openai",
             "tag_re": r"^v?\d+\.\d+\.\d+$", "impact": "engine"},
    "sglang": {"label": "SGLang 推理引擎", "kind": "pull", "hub": "lmsysorg/sglang",
               "tag_re": r"^v?\d+\.\d+\.\d+$", "impact": "engine"},
    "llamafactory": {"label": "LLaMA-Factory 训练引擎", "kind": "pull", "hub": "hiyouga/llamafactory",
                     "tag_re": r"^\d+\.\d+\.\d+$", "impact": "engine"},
    "gpu_agent": {"label": "GPU 监控 Agent (DCGM)", "kind": "pull", "hub": "nvidia/dcgm",
                  "tag_re": r"^\d+\.\d+\.\d+-ubuntu24\.04$", "impact": "agent",
                  "container": "fintune-gpu-agent-1"},
    # hub 为完整 registry 地址（ACR）→ check_latest 走 registry v2 匿名 token 流程
    "evalscope": {"label": "evalscope 评测引擎", "kind": "pull",
                  "hub": "modelscope-registry.cn-hangzhou.cr.aliyuncs.com/modelscope-repo/modelscope",
                  "tag_re": r"^ubuntu\d+\.\d+-(cuda[\d.]+-)?py\d+\.\d+-.+$",
                  "impact": "engine",
                  "hint": "官方开源 ModelScope 镜像（内置 evalscope）；拉取完成后新评测任务使用新 tag，运行中的评测任务不受影响"},
    "mineru": {"label": "MinerU 文档解析", "kind": "build", "ctx": "images/mineru",
               "image": "fintune/mineru:1.0", "impact": "agent",
               "container": "fintune-mineru-1",
               "hint": "MinerU 文档解析容器将以新镜像重建，解析服务中断数秒"},
    "backend": {"label": "平台后端", "kind": "build", "ctx": "platform/backend",
                "image": "fintune/backend:1.0", "container": "fintune-backend-1", "impact": "self"},
    "web": {"label": "Web 前端", "kind": "build", "ctx": "platform/frontend",
            "image": "fintune/web:1.0", "container": "fintune-web-1", "impact": "self"},
}


# ---------------------------------------------------------------------------
# 运行时 pin（/data/upgrade/image_pins.json，官方镜像升级后的运行时覆盖）
# ---------------------------------------------------------------------------

def _base_pins() -> dict:
    return {
        "vllm": settings.images_vllm,
        "sglang": settings.images_sglang,
        "llamafactory": settings.images_train,
        "gpu_agent": GPU_AGENT_IMAGE,
        "evalscope": settings.images_eval,
    }


def pins_path() -> str:
    return os.path.join(settings.data_dir, "upgrade", "image_pins.json")


def load_pins() -> dict:
    try:
        with open(pins_path(), encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_pins(pins: dict) -> None:
    p = pins_path()
    os.makedirs(os.path.dirname(p), exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(pins, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def current_image(key: str) -> str:
    """组件当前生效镜像（基础 pin + 运行时升级覆盖）；训练/推理/镜像页统一走这里"""
    base = _base_pins()
    if key not in base:
        raise KeyError(f"未知组件 {key}")
    return load_pins().get(key) or base[key]


def impact_hint(target: str) -> str:
    c = COMPONENTS.get(target, {})
    if c.get("hint"):
        return c["hint"]
    return {
        "engine": "拉取完成后新任务/新部署使用新镜像；运行中的任务与部署不受影响（重新部署后生效）",
        "agent": "GPU 监控 Agent 将以新镜像重建，监控数据中断数秒",
        "self": "该服务将以新镜像重建，平台短暂不可用（约 1 分钟）",
    }.get(c.get("impact", ""), "")


# ---------------------------------------------------------------------------
# 版本检查（Docker Hub）与状态汇总
# ---------------------------------------------------------------------------

_latest_cache: dict[str, tuple[float, list[str]]] = {}


def _semver_key(tag: str) -> tuple | None:
    m = re.match(r"^v?(\d+)\.(\d+)\.(\d+)$", tag)
    return tuple(int(x) for x in m.groups()) if m else None


def _hub_tags(hub: str) -> list[str]:
    """Docker Hub 组件（hub 为纯仓库名）：hub.docker.com tags API 首页 100 条"""
    import requests
    r = requests.get(f"{HUB}/{hub}/tags?page_size=100", timeout=30)
    r.raise_for_status()
    return [item.get("name", "") for item in r.json().get("results", [])]


def _registry_tags(hub: str) -> list[str]:
    """通用 registry（hub 为完整 域名/命名空间/仓库，如 ACR）：
    v2 挑战头（WWW-Authenticate）→ 匿名 token → tags/list 分页拉取。
    挑战头缺失/解析失败时裸试（部分 registry 匿名直接放行）；401 等错误向上抛"""
    import requests
    host, _, repo = hub.partition("/")
    base = f"https://{host}"
    headers = {}
    try:
        ch = requests.get(f"{base}/v2/", timeout=30).headers.get("WWW-Authenticate", "")
        m = re.match(r'Bearer realm="([^"]+)",\s*service="([^"]+)"', ch)
        if m:
            tr = requests.get(m.group(1),
                              params={"service": m.group(2), "scope": f"repository:{repo}:pull"},
                              timeout=30)
            body = tr.json() or {}
            tok = body.get("token") or body.get("access_token")
            if tok:
                headers["Authorization"] = f"Bearer {tok}"
    except Exception:
        pass
    tags: list[str] = []
    last = ""
    for _ in range(4):  # 每页 500，最多 4 页（2000 tag 足够）
        params = {"n": 500}
        if last:
            params["last"] = last
        r = requests.get(f"{base}/v2/{repo}/tags/list", params=params,
                         headers=headers, timeout=30)
        r.raise_for_status()
        batch = r.json().get("tags") or []
        tags.extend(batch)
        if len(batch) < 500:
            break
        last = batch[-1]
    return tags


def _tag_version_key(tag: str):
    """tag → 排序键：纯 semver tag 直接匹配（Docker Hub 组件）；
    镜像型 tag（如 ubuntu22.04-cuda12.4.0-py311-torch2.6.0-1.26.0-LLM）取 tag 内
    最后一个 semver 数段落作版本近似（modelscope 官方镜像的镜像版本）"""
    k = _semver_key(tag)
    if k:
        return k
    vers = re.findall(r"(\d+\.\d+(?:\.\d+)?)", tag)
    if vers:
        parts = [int(x) for x in vers[-1].split(".")]
        parts += [0] * (3 - len(parts))
        return tuple(parts[:3])
    return None


def check_latest(key: str, force: bool = False) -> list[str]:
    """拉取组件可用版本（降序前 10，缓存 10 分钟）。失败抛 RuntimeError。
    hub 为纯仓库名走 Docker Hub API；含域名（如 ACR 完整地址）走 registry v2 流程"""
    c = COMPONENTS.get(key)
    if not c or c["kind"] == "build":
        return []
    now = time.time()
    hit = _latest_cache.get(key)
    if not force and hit and now - hit[0] < _LATEST_TTL:
        return hit[1]
    hub = c["hub"]
    tags = _registry_tags(hub) if "." in hub.split("/")[0] else _hub_tags(hub)
    found = []
    for tag in tags:
        if re.match(c["tag_re"], tag):
            k = _tag_version_key(tag)
            if k:
                found.append((k, tag))
    found.sort(reverse=True)
    out = [t for _, t in found[:10]]
    _latest_cache[key] = (now, out)
    return out


def _local_info(image: str) -> tuple[bool, int]:
    from . import image_ops
    try:
        imgs = image_ops.docker_client().images.list(image)
        if imgs:
            return True, int(imgs[0].attrs.get("Size") or 0)
    except Exception:
        pass
    return False, 0


def _runtime(key: str) -> str:
    """运行时版本探测：引擎/agent 复用镜像页探测（5min 缓存）；自构组件显示镜像构建信息"""
    from . import taskcontainer
    probe_key = {"vllm": "vllm", "sglang": "sglang",
                 "llamafactory": "llamafactory", "gpu_agent": "gpu-agent",
                 "evalscope": "evalscope"}.get(key)
    if probe_key:
        from ..api import images as images_api
        try:
            img = current_image(key)
            for k, _fn, _comp, _desc, cmd in images_api.PLATFORM:
                if k == probe_key:
                    return images_api._probe(k, img, cmd)
        except Exception as e:
            return f"探测失败: {str(e)[:120]}"
        return ""
    try:
        imgs = taskcontainer.docker_client().images.list(COMPONENTS[key]["image"])
        if imgs:
            created = (imgs[0].attrs.get("Created") or "")[:19].replace("T", " ")
            return f"镜像 {imgs[0].short_id[:12]}（构建于 {created}）"
    except Exception:
        pass
    return "镜像未拉取"


def status() -> list[dict]:
    """各组件升级状态（当前 pin / 本地镜像 / 运行时版本 / 进行中任务）"""
    from ..database import SessionLocal
    busy: dict[str, int] = {}
    try:
        db = SessionLocal()
        for t in db.query(UpgradeTask).filter(UpgradeTask.status == "running").all():
            busy[t.target] = t.id
        db.close()
    except Exception:
        pass
    out = []
    for key, c in COMPONENTS.items():
        img = c.get("image") or current_image(key)
        exists, size = _local_info(img)
        out.append({
            "key": key, "label": c["label"], "image": img,
            "kind": c["kind"], "impact": c["impact"],
            "exists": exists, "size": size,
            "runtime": _runtime(key),
            "impact_hint": impact_hint(key),
            "running_task": busy.get(key, 0),
        })
    return out


# ---------------------------------------------------------------------------
# 容器蓝绿重建
# ---------------------------------------------------------------------------

def _spec_kwargs(attrs: dict, name: str, image: str) -> dict:
    """从现有容器 attrs 推导 docker-py containers.run 参数（镜像切换复用原配置）"""
    import docker
    conf = attrs.get("Config", {}) or {}
    hc = attrs.get("HostConfig", {}) or {}
    kwargs: dict = dict(
        image=image,
        name=name,
        command=conf.get("Cmd") or None,
        entrypoint=conf.get("Entrypoint") or None,
        environment=conf.get("Env"),
        labels=dict(conf.get("Labels") or {}),
        working_dir=conf.get("WorkingDir") or None,
        detach=True,
        auto_remove=False,
        stdout=True,
        stderr=True,
        network_mode=hc.get("NetworkMode") or "default",
        ipc_mode=hc.get("IpcMode") or None,
        # Binds 转 volumes dict（docker-py 7.x 的 containers.run 不接受 binds 关键字）
        volumes=_binds_to_volumes(hc.get("Binds")),
        restart_policy={"Name": (hc.get("RestartPolicy") or {}).get("Name") or "no"},
        cap_add=hc.get("CapAdd") or None,
        security_opt=hc.get("SecurityOpt") or None,
    )
    if hc.get("Privileged"):
        kwargs["privileged"] = True
    if hc.get("Memory"):
        kwargs["mem_limit"] = hc["Memory"]
    if hc.get("CpuQuota"):
        kwargs["cpu_quota"] = hc["CpuQuota"]
    if hc.get("CpuPeriod"):
        kwargs["cpu_period"] = hc["CpuPeriod"]
    ports = {}
    for cport, bindings in (hc.get("PortBindings") or {}).items():
        if bindings:
            ports[cport] = bindings[0].get("HostPort")
    if ports:
        kwargs["ports"] = ports
    dreqs = []
    for dr in (hc.get("DeviceRequests") or []):
        # 仅传非空项：driver 缺省显式补 "nvidia"（docker-py 默认值在不同版本
        # 为 ""/None，序列化进 API 可能 400）
        kw = {}
        kw["driver"] = dr.get("Driver") or "nvidia"
        if dr.get("Count") is not None:
            kw["count"] = dr["Count"]
        if dr.get("Capabilities"):
            kw["capabilities"] = dr["Capabilities"]
        if dr.get("DeviceIDs"):
            kw["device_ids"] = dr["DeviceIDs"]
        dreqs.append(docker.types.DeviceRequest(**kw))
    if dreqs:
        kwargs["device_requests"] = dreqs
    return kwargs


def _binds_to_volumes(binds: list | None) -> dict | None:
    """HostConfig.Binds（["/h:/c:rw", ...]）→ run() 的 volumes dict。
    路径按前两段切（host:container[:mode]），Linux 路径不含冒号。"""
    vols: dict = {}
    for b in binds or []:
        parts = b.split(":", 2)
        if len(parts) < 2:
            continue
        host, cpath = parts[0], parts[1]
        mode = parts[2] if len(parts) > 2 and parts[2] else "rw"
        vols[host] = {"bind": cpath, "mode": mode}
    return vols or None


def _net_names(attrs: dict) -> list[str]:
    """旧容器实际挂接的用户网络名（host/none/内置 bridge 除外）"""
    nets = (attrs.get("NetworkSettings") or {}).get("Networks") or {}
    return [n for n, info in nets.items()
            if n not in ("host", "none", "bridge") and (info or {}).get("NetworkID")]


def _service_aliases(attrs: dict, container_name: str) -> list[str]:
    """compose 服务名别名：新容器挂接网络时带上，保证服务名 DNS 解析不断"""
    labels = (attrs.get("Config", {}).get("Labels") or {})
    svc = labels.get("com.docker.compose.service", "")
    out = [a for a in (svc, container_name) if a]
    return out


def _wait_running(ctr, timeout: int = 120) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        ctr.reload()
        if ctr.status == "running":
            return
        if ctr.status == "exited":
            try:
                tail = ctr.logs(tail=20).decode("utf-8", "ignore")[-500:]
            except Exception:
                tail = ""
            raise RuntimeError(f"容器启动即退出: {tail}")
        time.sleep(2)
    raise RuntimeError(f"容器 {timeout}s 内未 running")


def recreate_container(name: str, image: str) -> str:
    """蓝绿重建：先建临时名新容器（带服务别名）并验证 running，再停旧删旧改名。
    返回新容器 id。backend/web 自升级时，旧容器停止会连带杀掉调用方进程，
    改名可能未完成——由新进程 converge() 兜底收敛。"""
    from . import taskcontainer
    d = taskcontainer.docker_client()
    old = d.containers.get(name)
    attrs = old.attrs
    kwargs = _spec_kwargs(attrs, name + "-upgrade", image)
    nets = _net_names(attrs)
    if nets:
        kwargs["network_mode"] = "none"  # 先不挂网络，连接时带服务别名
    labels = kwargs.get("labels") or {}
    labels["fintune.upgrade-temp"] = "1"
    kwargs["labels"] = labels
    try:
        new = d.containers.run(**kwargs)
    except Exception:
        # run() 内部 start 阶段失败（如 GPU 驱动缺失致 prestart hook 报错）时，
        # 容器已处于 Created 态且拿不到 new 引用——按临时名清理防泄漏
        try:
            d.containers.get(name + "-upgrade").remove(force=True)
        except Exception:
            pass
        raise
    try:
        if nets:
            alias_list = _service_aliases(attrs, name)
            for nname in nets:
                net = d.networks.get(nname)
                try:
                    net.connect(new.id, aliases=alias_list)
                except Exception:
                    try:
                        net.disconnect(new.id)
                        net.connect(new.id, aliases=alias_list)
                    except Exception:
                        log.exception("network alias setup failed for %s", nname)
        _wait_running(new)
    except Exception:
        try:
            new.remove(force=True)
        except Exception:
            pass
        raise
    try:
        old.stop(timeout=10)
        old.remove(force=True)
        new.rename(name)
    except Exception:
        log.exception("switchover partial failure for %s（新容器已 running，等待 converge 收敛）", name)
    return new.id


# ---------------------------------------------------------------------------
# 升级任务
# ---------------------------------------------------------------------------

def _log(db, task: UpgradeTask, msg: str) -> None:
    line = f"[{datetime.now().strftime('%H:%M:%S')}] {msg}"
    task.log = ((task.log or "") + line + "\n")[-20000:]
    db.commit()


def _finish(db, task: UpgradeTask, status_: str) -> None:
    task.status = status_
    task.finished_at = datetime.utcnow()
    db.commit()


def execute(db, target: str, to_version: str, user_id: int) -> UpgradeTask:
    """创建升级任务并后台执行。同 target 已有 running 任务时拒绝（幂等保护）"""
    c = COMPONENTS.get(target)
    if not c:
        raise ValueError(f"未知升级目标: {target}")
    if c["kind"] == "pull":
        to_version = (to_version or "").strip()
        if not to_version:
            raise ValueError("引擎/Agent 镜像升级需指定目标版本（to_version）")
        if not re.match(c["tag_re"], to_version):
            raise ValueError(f"目标版本 {to_version} 不符合该组件 tag 格式")
    from_version = c.get("image") or current_image(target)
    running = db.query(UpgradeTask).filter_by(target=target, status="running").first()
    if running:
        raise ValueError(f"{target} 已有升级任务进行中（#{running.id}），请等待其完成")
    task = UpgradeTask(target=target, from_version=from_version[:160],
                       to_version=(to_version or "重建")[:160],
                       status="running", created_by=user_id)
    db.add(task)
    db.commit()
    db.refresh(task)
    threading.Thread(target=_worker_upgrade, args=(task.id, target, to_version),
                     name=f"upgrade-{target}", daemon=True).start()
    return task


def _worker_upgrade(tid: int, target: str, to_version: str) -> None:
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        task = db.get(UpgradeTask, tid)
        c = COMPONENTS[target]
        _log(db, task, f"=== 开始升级 {target}：{task.from_version} → {to_version or '重建'}")
        img = f"{c['hub']}:{to_version}" if c["kind"] == "pull" else c["image"]
        # 1) 获取镜像（pull / 以 /repo 源码树重建）
        _log(db, task, f"获取镜像 {img}（大镜像可能需数十分钟）…")
        try:
            from . import image_ops
            if c["kind"] == "pull":
                image_ops.docker_client().images.pull(img)
            else:
                ctx = os.path.join(image_ops.REPO_DIR, c["ctx"])
                image_ops.docker_client().images.build(path=ctx, tag=img, rm=True)
            _log(db, task, f"镜像就绪: {img}")
        except Exception as e:
            _log(db, task, f"✗ 镜像获取失败: {str(e)[:500]}")
            _finish(db, task, "failed")
            return
        # 2) 应用：引擎镜像更新 pin；agent/self 蓝绿重建容器
        try:
            if c["impact"] == "engine":
                pins = load_pins()
                pins[target] = img
                save_pins(pins)
                _log(db, task, f"pin 已更新: {target} → {img}（新任务/部署将使用新镜像）")
            else:
                _log(db, task, f"蓝绿重建容器 {c['container']}…")
                recreate_container(c["container"], img)
                _log(db, task, f"容器已切换到 {img}")
        except Exception as e:
            _log(db, task, f"✗ 应用失败: {str(e)[:500]}")
            _finish(db, task, "failed")
            return
        _finish(db, task, "success")
        _log(db, task, "=== 升级完成")
    except Exception as e:
        try:
            task = db.get(UpgradeTask, tid)
            if task and task.status == "running":
                _log(db, task, f"✗ 升级异常: {str(e)[:500]}")
                _finish(db, task, "failed")
        except Exception:
            log.exception("upgrade task %s epilogue failed", tid)
    finally:
        db.close()


def converge(db) -> None:
    """自升级任务收敛（backend/web 蓝绿切换会杀掉 worker 进程）：
    新进程每轮调度检查 running 的 self 任务：
    - 临时容器 running + 旧容器 running：worker 正在停旧容器，等下轮
    - 临时容器 running + 旧容器已退出/消失：删旧补改名，判成功
      （backend 自升级时 worker 恰在 old.stop 时被杀，必须由此兜底）
    - 临时容器不存在 + 原容器 running：worker 已完成改名，判成功
    - 超过 15 分钟无果：判失败（残留临时容器 *-upgrade 需人工清理）"""
    from datetime import timezone
    from . import taskcontainer
    for t in db.query(UpgradeTask).filter(UpgradeTask.status == "running").all():
        c = COMPONENTS.get(t.target)
        if not c or c["impact"] != "self":
            continue
        try:
            d = taskcontainer.docker_client()
            temp_name = c["container"] + "-upgrade"
            new = None
            try:
                ctr = d.containers.get(temp_name)
                if ctr.status == "running":
                    new = ctr
            except Exception:
                pass
            if new:
                old = None
                try:
                    old = d.containers.get(c["container"])
                except Exception:
                    pass
                if old is not None and old.status == "running":
                    # worker 切换进行中；但若长时间卡死（worker 意外死亡且旧容器
                    # 未被停），15 分钟后判失败，服务仍由旧容器承载
                    age = time.time() - t.created_at.replace(tzinfo=timezone.utc).timestamp()
                    if age > 900:
                        _log(db, t, "✗ 切换卡死超时（临时容器已 running 但旧容器未停止），判失败")
                        _finish(db, t, "failed")
                    continue
                if old is not None:
                    try:
                        old.remove(force=True)
                        log.info("upgrade converge: removed stale %s", c["container"])
                    except Exception:
                        pass
                try:
                    new.rename(c["container"])
                    log.info("upgrade converge: renamed %s -> %s", temp_name, c["container"])
                except Exception:
                    pass
                _finish(db, t, "success")
                continue
            # 临时容器不存在：原容器活着 = 改名已随 worker 完成
            try:
                orig = d.containers.get(c["container"])
                if orig.status == "running":
                    _finish(db, t, "success")
                    continue
            except Exception:
                pass
            age = time.time() - t.created_at.replace(tzinfo=timezone.utc).timestamp()
            if age > 900:
                _log(db, t, "✗ 收敛超时（15 分钟无果）；如残留 *-upgrade 临时容器请人工清理")
                _finish(db, t, "failed")
        except Exception:
            db.rollback()
            log.exception("upgrade converge error for task %s", t.id)

"""引擎镜像按需操作（懒加载，部署时不预下载）

用户要求：vllm/sglang/llamafactory 镜像不在部署阶段下载，页面上创建
训练/推理任务时才检测本地是否已有镜像，缺失才拉取（官方开源镜像 pull；
fintune/* 自构镜像以宿主源码树 /repo 上下文流式 build）。

- ensure_image(image) 幂等：已存在→ready；缺失→后台线程拉取/构建并返回 pulling
- 拉取/构建失败记录 error，重试可再次调用 ensure_image
- 调用方（调度器/启动线程）每轮轮询 ensure_image 直到 ready 再创建容器
"""
import os
import logging
import threading

from ..config import settings

log = logging.getLogger("image_ops")

REPO_DIR = os.environ.get("VERVEDECK_REPO", "/repo")  # compose 挂载宿主源码树（只读）

# 官方开源镜像前缀 → 缺失时 docker pull；其余（fintune/*）走 /repo 上下文 build
# vllm/ lmsysorg/ hiyouga/(LLaMA-Factory 官方预构建) nvidia/(DCGM 等) 均为官方镜像
OFFICIAL_PREFIXES = ("vllm/", "lmsysorg/", "hiyouga/", "nvidia/")

PULL_TIMEOUT = 7200  # 大镜像（vllm 35GB）拉取上限 2h


class ImageNotReady(Exception):
    """镜像未就绪（拉取中/拉取失败），任务创建方应等待重试而非判失败"""


_lock = threading.Lock()
# image -> {"kind": "pull"|"build", "status": "running"|"ok"|"failed", "error": str}
_INFLIGHT: dict[str, dict] = {}


def docker_client():
    from .taskcontainer import docker_client as _dc
    return _dc()


def image_exists(image: str) -> bool:
    try:
        return bool(docker_client().images.list(image))
    except Exception:
        return False


def _ctx_dir(image: str) -> str | None:
    """自构镜像（fintune/<repo>:tag）的构建上下文：/repo/images/<repo>"""
    if not image.startswith("fintune/"):
        return None
    repo = image.split("/")[1].split(":")[0]
    ctx = os.path.join(REPO_DIR, "images", repo)
    if os.path.isdir(ctx) and os.path.exists(os.path.join(ctx, "Dockerfile")):
        return ctx
    return None


def _kind(image: str) -> str:
    if any(image.startswith(p) for p in OFFICIAL_PREFIXES):
        return "pull"
    return "build" if _ctx_dir(image) else "pull"


def ensure_image(image: str, force: bool = False) -> str:
    """检测镜像；缺失（或 force）则启动后台 pull/build。

    返回: ready | pulling | failed
    """
    if image_exists(image) and not force:
        return "ready"
    with _lock:
        st = _INFLIGHT.get(image)
        if st and st["status"] == "running":
            return "pulling"
        if st and st["status"] == "failed" and not force:
            return "failed"
        kind = _kind(image)
        _INFLIGHT[image] = {"kind": kind, "status": "running", "error": ""}
    threading.Thread(target=_worker, args=(image, kind),
                     name=f"img-{os.path.basename(image)}", daemon=True).start()
    log.info("image %s -> background %s (force=%s)", image, kind, force)
    return "pulling"


def _worker(image: str, kind: str) -> None:
    d = docker_client()
    try:
        if kind == "pull":
            d.images.pull(image)
        else:
            ctx = _ctx_dir(image)
            if not ctx:  # 兜底：上下文缺失时退回 pull
                d.images.pull(image)
            else:
                # path= 方式由 docker-py 内部构建上下文 tar（fileobj 自打包的
                # PAX 头会被 BuildKit 误当 Dockerfile 解析导致 400）
                d.images.build(path=ctx, tag=image, rm=True)
        with _lock:
            _INFLIGHT[image] = {"kind": kind, "status": "ok", "error": ""}
        log.info("image %s %s done", image, kind)
    except Exception as e:
        with _lock:
            _INFLIGHT[image] = {"kind": kind, "status": "failed", "error": str(e)[:300]}
        log.exception("image %s %s failed", image, kind)


def image_status(image: str) -> dict:
    """查询镜像状态（供 API 展示）：exists + 后台操作状态"""
    with _lock:
        op = dict(_INFLIGHT.get(image) or {"kind": "", "status": "idle", "error": ""})
    op["exists"] = image_exists(image)
    return op


def reset(image: str) -> None:
    """清除失败记录，允许下次 ensure_image 重新触发"""
    with _lock:
        st = _INFLIGHT.get(image)
        if st and st["status"] == "failed":
            _INFLIGHT.pop(image, None)

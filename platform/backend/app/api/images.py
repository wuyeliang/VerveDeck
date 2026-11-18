"""镜像管理：平台全量镜像清单 + 版本探测 + 拉取/重建 + 自定义镜像（国产芯片等）

清单唯一数据源是 upgrade_service.COMPONENTS（19 项 = 5 引擎 + 3 自构 +
11 监控/基础设施），本页面与集群升级页按同一顺序渲染，两页清单完全一致。

- 官方开源镜像（pull 类）：refresh = docker pull
- 自构镜像（fintune/*）：refresh = 以 compose 挂载的 /repo（宿主源码树，只读）
  为上下文通过 docker socket 流式构建
- 版本探测：一次性容器内执行组件 probe 命令读版本，结果缓存 5 分钟；
  无 shell 的镜像（distroless）显示镜像 tag，自构镜像显示构建信息
- 拉取/重建均为后台异步（image_ops），页面轮询 pull_status 展示进度状态
"""
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..security import require_role
from ..models import CustomImage, User
from ..services import image_ops
from ..services import upgrade_service

router = APIRouter(prefix="/api/images", tags=["images"])

PROBE_TTL = 300

_probe_cache: dict[str, tuple[float, str]] = {}


def _admin(user: User = Depends(require_role("super_admin"))):
    return user


def _docker():
    # 复用全局单例 client（每次 from_env 会新建 API 会话，列表页一次请求多次调用会叠加开销）
    from ..services.taskcontainer import docker_client
    return docker_client()


def _image_info(image: str) -> dict:
    out = {"exists": False, "size": 0}
    try:
        imgs = _docker().images.list(image)
        if imgs:
            out["exists"] = True
            out["size"] = int(imgs[0].short_id and imgs[0].attrs.get("Size") or 0)
    except Exception:
        pass
    return out


# 探测结果双缓存：
# - _probe_cache：TTL 缓存（PROBE_TTL 内直接命中，零开销）
# - _probe_stale：无 TTL 兜底值（过期/首探未完成时立即展示旧值，避免页面空转）
# 探测本体（起容器 import 引擎包，vllm 可达数十秒）只跑在后台线程，
# 列表/状态接口永不阻塞——否则 backend 重启后首开页面要等 1-3 分钟才出数据。
_probe_stale: dict[str, str] = {}
_probe_busy: set[str] = set()
_probe_lock = threading.Lock()


def _probe_sync(key: str, image: str, cmd: str) -> str:
    """执行一次真实探测并写双缓存（供后台线程/手动刷新调用，可能阻塞数十秒）"""
    now = time.time()
    try:
        d = _docker()
        if not d.images.list(image):
            res = "镜像未拉取"
        else:
            # detach=True 才返回容器对象（非 detach 时 run 直接返回 bytes 输出流，
            # 且非零退出会抛 ContainerError）；wait 结束后取日志，finally 保证清理不泄漏
            ctr = d.containers.run(image, entrypoint=["sh", "-c", cmd],
                                   detach=True, stdout=True, stderr=True, remove=False)
            try:
                try:
                    ctr.wait(timeout=90)
                except Exception:
                    pass
                out = ctr.logs(stdout=True, stderr=False).decode("utf-8", "ignore").strip()
            finally:
                try:
                    ctr.remove(force=True)
                except Exception:
                    pass
            res = out.split("\n")[-1].strip() if out else "探测无输出"
    except Exception as e:
        res = f"探测失败: {str(e)[:160]}"
    _probe_cache[key] = (now, res)
    _probe_stale[key] = res
    return res


def _probe_bg(key: str, image: str, cmd: str) -> None:
    try:
        _probe_sync(key, image, cmd)
    finally:
        with _probe_lock:
            _probe_busy.discard(key)


def _probe(key: str, image: str, cmd: str, force: bool = False) -> str:
    now = time.time()
    hit = _probe_cache.get(key)
    if not force and hit and now - hit[0] < PROBE_TTL:
        return hit[1]
    if force:
        # 用户点「探测」按钮：显式刷新，同步等待新结果
        return _probe_sync(key, image, cmd)
    # 缓存过期或首探未完成：立即返回（旧值兜底 / 探测中占位），后台刷新
    with _probe_lock:
        if key not in _probe_busy:
            _probe_busy.add(key)
            threading.Thread(target=_probe_bg, args=(key, image, cmd),
                             name=f"probe-{key}", daemon=True).start()
    return _probe_stale.get(key) or "探测中…"


@router.get("")
def list_images(user=Depends(_admin)):
    """平台全量镜像清单（与集群升级页同一数据源、同序，19 项）+ 自定义镜像"""
    items = []
    for key, c in upgrade_service.COMPONENTS.items():
        image = upgrade_service.component_image(key)
        info = _image_info(image)
        # 自构镜像统一 fintune/* 前缀；其余（Docker Hub 或 ACR 完整地址）均为官方开源
        source = "selfbuilt" if image.startswith("fintune/") else "official"
        if c.get("probe"):
            version = _probe(key, image, c["probe"])
        else:
            # 无探测命令：自构组件显示构建信息，distroless 显示镜像 tag
            version = upgrade_service._runtime(key)
        items.append({
            "key": key, "image": image, "component": c["label"], "desc": c["desc"],
            "source": source, "exists": info["exists"], "size": info["size"],
            "version": version,
            "pull_status": image_ops.image_status(image),
        })
    customs = [
        {"key": f"custom-{c.id}", "image": c.name, "component": "自定义镜像",
         "desc": c.note, "source": "custom",
         "exists": c.status == "present", "size": c.size or 0, "version": c.status}
        for c in _docker_list_customs()
    ]
    return items + customs


def _customs():
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        return db.query(CustomImage).order_by(CustomImage.id.asc()).all()
    finally:
        db.close()


def _docker_list_customs():
    return _customs()


class CustomReq(BaseModel):
    name: str
    note: str = ""


@router.get("/custom")
def list_custom(user=Depends(_admin)):
    return [_custom_row(c) for c in _customs()]


def _custom_row(c: CustomImage) -> dict:
    info = _image_info(c.name)
    return {"id": c.id, "name": c.name, "note": c.note,
            "status": "present" if info["exists"] else "missing",
            "size": info["size"], "created_at": str(c.created_at)}


@router.post("/custom")
def add_custom(req: CustomReq, user=Depends(_admin)):
    name = (req.name or "").strip()
    if ":" not in name:
        raise HTTPException(400, "镜像需为 name:tag 形式（如 ascend/vllm-ascend:latest）")
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        if db.query(CustomImage).filter(CustomImage.name == name).first():
            raise HTTPException(400, "已登记该镜像")
        c = CustomImage(name=name, note=req.note[:255])
        db.add(c)
        db.commit()
        db.refresh(c)
        return _custom_row(c)
    except HTTPException:
        raise
    finally:
        db.close()


@router.delete("/custom/{cid}")
def delete_custom(cid: int, user=Depends(_admin)):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        c = db.get(CustomImage, cid)
        if not c:
            raise HTTPException(404, "不存在")
        db.delete(c)
        db.commit()
        return {"ok": True}
    except HTTPException:
        raise
    finally:
        db.close()


class BatchDeleteReq(BaseModel):
    ids: list[int]


@router.post("/custom/batch-delete")
def batch_delete_custom(req: BatchDeleteReq, user=Depends(_admin)):
    """批量移除自定义镜像登记：逐个执行单删逻辑，返回部分结果"""
    deleted: list[int] = []
    failed: list[dict] = []
    for cid in req.ids:
        try:
            delete_custom(cid, user=user)
            deleted.append(cid)
        except HTTPException as e:
            failed.append({"id": cid, "error": str(e.detail)})
    return {"deleted": deleted, "failed": failed}


@router.post("/{key}/probe")
def probe(key: str, user=Depends(_admin)):
    """强制刷新版本探测（缓存失效）；无探测命令的组件返回当前运行时版本"""
    c = upgrade_service.COMPONENTS.get(key)
    if not c:
        raise HTTPException(404, "未知镜像组件")
    if not c.get("probe"):
        return {"ok": True, "version": upgrade_service._runtime(key)}
    image = upgrade_service.component_image(key)
    return {"ok": True, "version": _probe(key, image, c["probe"], force=True)}


@router.post("/{key}/refresh")
def refresh(key: str, force: bool = False, user=Depends(_admin)):
    """后台拉取/重建（异步，立即返回）：
    - 官方镜像 docker pull；自构镜像以 /repo 上下文流式 build
    - force=true 且镜像已存在时强制重新拉取/重建（版本升级用）
    进度状态通过 GET /api/images 的 pull_status 轮询。
    """
    c = upgrade_service.COMPONENTS.get(key)
    if not c:
        raise HTTPException(404, "未知镜像组件")
    image = upgrade_service.component_image(key)
    st = image_ops.ensure_image(image, force=force)
    if st == "failed":
        raise HTTPException(502, f"操作失败: {image_ops.image_status(image).get('error', '')}")
    # 拉取/重建后旧版本值已失效：双缓存同清，下次列表触发后台重探（显示「探测中…」）
    _probe_cache.pop(key, None)
    _probe_stale.pop(key, None)
    return {"ok": True, "action": st, "image": image}

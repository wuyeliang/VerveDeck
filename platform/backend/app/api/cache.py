"""缓存加速：缓存服务实例（托管容器）生命周期 + 部署关联端点注入

独立于推理部署的托管容器（KV 缓存存储等，现成镜像或自定义启动命令）。
LMCache 编排型（config 非空）：版本/内存/淘汰/L2 存 config，启动时渲染
lmcache server 命令与镜像（cache_service.render_spec）；规格摘要不一致 = 漂移。
command 留空 = 按镜像默认入口直跑；非空 = pause 占位 + 容器内 exec（日志/退出码可查）。
部署关联（Deployment.cache_service_id）后，其推理实例注入 VD_CACHE_ENDPOINT=http://ip:port。
状态收敛由调度器 tick（cache_service.reconcile）驱动；本页只管生命周期操作。
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..security import get_current_user
from ..models import CacheService, Deployment
from ..services import cache_service as cs

log = logging.getLogger("cache")
router = APIRouter(tags=["cache"])

_EDITABLE = ("description", "image", "command", "env", "port",
             "cache_type", "capacity_gb",
             "provider", "provider_version", "mode", "config",
             "metrics_port", "restart_on_error")
_CACHE_TYPES = ("memory", "ssd")
_PROVIDERS = ("lmcache",)
_MODES = ("managed", "external")


def _used_by(db: Session, cid: int) -> list[str]:
    """引用方：部署（cache_service_id）+ 路由（route.cache_service_id）。"""
    from ..models import ModelRoute
    out = [d.name for d in db.query(Deployment).filter_by(cache_service_id=cid).all()]
    out += [f"路由:{r.name}" for r in
            db.query(ModelRoute).filter_by(cache_service_id=cid).all()]
    return out


def _brief(c: CacheService, db: Session | None = None) -> dict:
    out = {
        "id": c.id, "name": c.name, "description": c.description or "",
        "cache_type": c.cache_type or "memory",
        "capacity_gb": c.capacity_gb or 0,
        "image": c.image, "command": c.command or "", "env": c.env or {},
        "port": c.port or 0, "status": c.status,
        "state_message": c.state_message or "",
        "endpoint": c.endpoint or "",
        "url": f"http://{c.endpoint}" if c.endpoint else "",
        "container_id": c.container_id or "",
        # LMCache 编排（config 非空 = 编排型；存量空 = 旧路径/逻辑型）
        "provider": c.provider or "lmcache",
        "provider_version": c.provider_version or "",
        "mode": c.mode or "managed",
        "config": c.config or {},
        "metrics_port": c.metrics_port or 0,
        "restart_on_error": bool(c.restart_on_error),
        "healthy": bool(c.healthy),
        "last_check_at": str(c.last_check_at) if c.last_check_at else "",
        "spec_digest": c.spec_digest or "",
        # 漂移 = 运行/启动中且规格摘要与容器启动时不一致（须重建才生效）
        "spec_drift": (c.status in ("running", "creating")) and bool(c.spec_digest)
        and c.spec_digest != cs.spec_digest_of(c),
        "created_by": c.created_by, "created_at": str(c.created_at),
    }
    if db is not None:
        out["used_by"] = _used_by(db, c.id)
    return out


def _validate(name: str, image: str, port, cache_type: str = "memory",
              capacity_gb=None) -> tuple[str, str, int, str, int]:
    """image 可为空 = 逻辑型缓存（仅名称/类型/容量元数据，不起容器、
    无端点注入；需端点时在高级设置填镜像后启动）。"""
    name = (name or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(400, "名称长度须 1-128")
    image = (image or "").strip()
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "端口须为整数")
    if port < 0 or port > 65535:
        raise HTTPException(400, "端口须 0-65535（0=不做端口探活）")
    cache_type = (cache_type or "memory").strip()
    if cache_type not in _CACHE_TYPES:
        raise HTTPException(400, "类型须为 memory（内存）/ ssd（SSD）")
    try:
        capacity_gb = max(0, int(capacity_gb or 0))
    except (TypeError, ValueError):
        raise HTTPException(400, "容量须为非负整数（GB）")
    return name, image, port, cache_type, capacity_gb


# ---------- CRUD ----------

@router.get("/api/cache/services")
def list_cache_services(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return [_brief(c, db) for c in db.query(CacheService).order_by(CacheService.id).all()]


class CacheReq(BaseModel):
    name: str
    description: str = ""
    cache_type: str = "memory"  # memory=内存 / ssd=SSD
    capacity_gb: int = 0  # 容量（GB，规划值）
    image: str = ""
    command: str = ""
    env: dict = {}
    port: int = 0
    # LMCache 编排（config 非空 = 编排型，版本/内存/淘汰/L2 存 config）
    provider: str = "lmcache"
    provider_version: str = ""
    mode: str = "managed"  # managed=托管 / external=外部（预留）
    config: dict = {}
    metrics_port: int = 0
    restart_on_error: bool = True


def _validate_orchestration(provider, provider_version, mode, metrics_port) -> tuple[str, str, str, int]:
    provider = (provider or "lmcache").strip() or "lmcache"
    if provider not in _PROVIDERS:
        raise HTTPException(400, "不支持的缓存提供方（现仅 lmcache）")
    mode = (mode or "managed").strip() or "managed"
    if mode not in _MODES:
        raise HTTPException(400, "模式须为 managed（托管）/ external（外部）")
    try:
        metrics_port = int(metrics_port or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "metrics 端口须为整数")
    if metrics_port < 0 or metrics_port > 65535:
        raise HTTPException(400, "metrics 端口须 0-65535（0=默认 8080）")
    return provider, (provider_version or "").strip(), mode, metrics_port


@router.post("/api/cache/services")
def create_cache_service(req: CacheReq, db: Session = Depends(get_db), user=Depends(get_current_user)):
    name, image, port, cache_type, capacity_gb = _validate(
        req.name, req.image, req.port, req.cache_type, req.capacity_gb)
    provider, version, mode, metrics_port = _validate_orchestration(
        req.provider, req.provider_version, req.mode, req.metrics_port)
    if db.query(CacheService).filter_by(name=name).first():
        raise HTTPException(400, f"缓存服务 {name} 已存在")
    c = CacheService(name=name, description=req.description or "", image=image,
                     cache_type=cache_type, capacity_gb=capacity_gb,
                     command=(req.command or "").strip(),
                     env={str(k): str(v) for k, v in (req.env or {}).items()},
                     port=port, status="stopped", created_by=user.id,
                     provider=provider, provider_version=version, mode=mode,
                     config={str(k): v for k, v in (req.config or {}).items()},
                     metrics_port=metrics_port,
                     restart_on_error=bool(req.restart_on_error))
    db.add(c)
    db.commit()
    db.refresh(c)
    return _brief(c, db)


@router.put("/api/cache/services/{cid}")
def update_cache_service(cid: int, req: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    c = db.get(CacheService, cid)
    if not c:
        raise HTTPException(404, "缓存服务不存在")
    req = {k: v for k, v in (req or {}).items() if v is not None}
    bad = [k for k in req if k not in _EDITABLE]
    if bad:
        raise HTTPException(400, f"不支持的字段: {', '.join(bad)}（名称不可修改）")
    if not req:
        raise HTTPException(400, "无更新字段")
    if "provider" in req or "mode" in req or "metrics_port" in req:
        provider, version, mode, metrics_port = _validate_orchestration(
            req.get("provider", c.provider),
            req.get("provider_version", c.provider_version),
            req.get("mode", c.mode), req.get("metrics_port", c.metrics_port))
        req = {**req, "provider": provider, "provider_version": version,
               "mode": mode, "metrics_port": metrics_port}
    _, image, port, cache_type, capacity_gb = _validate(
        c.name, req.get("image", c.image), req.get("port", c.port),
        req.get("cache_type", c.cache_type), req.get("capacity_gb", c.capacity_gb))
    for k, v in req.items():
        if k == "env":
            c.env = {str(a): str(b) for a, b in (v or {}).items()}
        elif k == "config":
            c.config = {str(a): b for a, b in (v or {}).items()}
        elif k == "command":
            c.command = (v or "").strip()
        elif k == "image":
            c.image = image
        elif k == "cache_type":
            c.cache_type = cache_type
        elif k == "capacity_gb":
            c.capacity_gb = capacity_gb
        elif k == "provider_version":
            c.provider_version = (v or "").strip()
        else:
            setattr(c, k, v)
    # 运行中改容器配置：
    #  - 旧路径（config 空）：镜像/命令/env/端口 变更 → 自动重启（保留既有行为）
    #  - 编排型（config 非空）：规格变更不自动重启，仅标漂移（spec_drift），
    #    停后重启动画才重建；顶层 image/command/env/port 不入渲染规格，改了也不重启
    orchestrated = bool(c.config)
    legacy_change = set(req) & {"image", "command", "env", "port"}
    orch_change = set(req) & {"config", "provider_version", "metrics_port"}
    if c.status in ("running", "creating") and legacy_change and not orchestrated:
        cs.stop_container(c)
        c.endpoint = ""
        c.container_id = ""
        if (c.image or "").strip():
            c.status = "creating"
            c.state_message = "配置变更，重启中…"
            db.commit()
            cs.launch_cache_thread(c.id, note="配置变更，重启中…")
        else:
            c.status = "stopped"
            c.state_message = ""
            db.commit()
    else:
        if c.status in ("running", "creating") and orch_change and orchestrated:
            c.state_message = "配置已变更（存在漂移），停止后重启生效"
        db.commit()
    return _brief(c, db)


@router.delete("/api/cache/services/{cid}")
def delete_cache_service(cid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    c = db.get(CacheService, cid)
    if not c:
        raise HTTPException(404, "缓存服务不存在")
    used = _used_by(db, c.id)
    if used:
        raise HTTPException(409, f"缓存服务被部署引用（{', '.join(used[:5])}…），不可删除")
    cs.stop_container(c)
    db.delete(c)
    db.commit()
    return {"ok": True}


# ---------- 生命周期 ----------

@router.post("/api/cache/services/{cid}/start")
def start_cache_service(cid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    c = db.get(CacheService, cid)
    if not c:
        raise HTTPException(404, "缓存服务不存在")
    if not cs.render_spec(c)["image"]:
        raise HTTPException(400, "逻辑型缓存（未配置镜像）无容器可启动；如需缓存端点请配置缓存提供方或镜像")
    if c.status in ("creating", "running"):
        raise HTTPException(400, "缓存服务正在启动/运行中")
    c.status = "creating"
    c.state_message = "启动中，等待镜像…"
    c.endpoint = ""
    c.container_id = ""
    db.commit()
    cs.launch_cache_thread(c.id)
    return _brief(c, db)


@router.post("/api/cache/services/{cid}/stop")
def stop_cache_service(cid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    c = db.get(CacheService, cid)
    if not c:
        raise HTTPException(404, "缓存服务不存在")
    cs.stop_container(c)  # 内部换发令牌作废旧启动线程
    c.status = "stopped"
    c.state_message = ""
    c.endpoint = ""
    c.container_id = ""
    db.commit()
    return _brief(c, db)

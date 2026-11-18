"""缓存加速：缓存服务实例（托管容器）生命周期 + 部署关联端点注入

独立于推理部署的托管容器（KV 缓存存储等，现成镜像或自定义启动命令）。
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

_EDITABLE = ("description", "image", "command", "env", "port")


def _used_by(db: Session, cid: int) -> list[str]:
    return [d.name for d in db.query(Deployment).filter_by(cache_service_id=cid).all()]


def _brief(c: CacheService, db: Session | None = None) -> dict:
    out = {
        "id": c.id, "name": c.name, "description": c.description or "",
        "image": c.image, "command": c.command or "", "env": c.env or {},
        "port": c.port or 0, "status": c.status,
        "state_message": c.state_message or "",
        "endpoint": c.endpoint or "",
        "url": f"http://{c.endpoint}" if c.endpoint else "",
        "container_id": c.container_id or "",
        "created_by": c.created_by, "created_at": str(c.created_at),
    }
    if db is not None:
        out["used_by"] = _used_by(db, c.id)
    return out


def _validate(name: str, image: str, port) -> tuple[str, str, int]:
    name = (name or "").strip()
    if not name or len(name) > 128:
        raise HTTPException(400, "名称长度须 1-128")
    image = (image or "").strip()
    if not image:
        raise HTTPException(400, "镜像不可为空")
    try:
        port = int(port or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "端口须为整数")
    if port < 0 or port > 65535:
        raise HTTPException(400, "端口须 0-65535（0=不做端口探活）")
    return name, image, port


# ---------- CRUD ----------

@router.get("/api/cache/services")
def list_cache_services(db: Session = Depends(get_db), user=Depends(get_current_user)):
    return [_brief(c, db) for c in db.query(CacheService).order_by(CacheService.id).all()]


class CacheReq(BaseModel):
    name: str
    description: str = ""
    image: str
    command: str = ""
    env: dict = {}
    port: int = 0


@router.post("/api/cache/services")
def create_cache_service(req: CacheReq, db: Session = Depends(get_db), user=Depends(get_current_user)):
    name, image, port = _validate(req.name, req.image, req.port)
    if db.query(CacheService).filter_by(name=name).first():
        raise HTTPException(400, f"缓存服务 {name} 已存在")
    c = CacheService(name=name, description=req.description or "", image=image,
                     command=(req.command or "").strip(),
                     env={str(k): str(v) for k, v in (req.env or {}).items()},
                     port=port, status="stopped", created_by=user.id)
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
    _, image, port = _validate(c.name, req.get("image", c.image), req.get("port", c.port))
    for k, v in req.items():
        if k == "env":
            c.env = {str(a): str(b) for a, b in (v or {}).items()}
        elif k == "command":
            c.command = (v or "").strip()
        elif k == "image":
            c.image = image
        else:
            setattr(c, k, v)
    # 运行中改配置：容器需重启才生效 → 停旧 + 后台重启（状态机交 reconcile 收敛）
    if c.status in ("running", "creating"):
        cs.stop_container(c)
        c.status = "creating"
        c.state_message = "配置变更，重启中…"
        c.endpoint = ""
        c.container_id = ""
        db.commit()
        cs.launch_cache_thread(c.id, note="配置变更，重启中…")
    else:
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

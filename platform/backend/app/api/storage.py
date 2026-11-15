"""存储管理：多类型存储后端注册（对象/本地/NFS/块存储）+ 对象浏览/删除

对象存储同步队列已整体移除（前端 UI、API 端点、StorageQueue 模型、同步留痕均不再保留）。
local/object 类型真实可用；nfs/block 为注册 + 状态占位（探测宿主挂载点路径）。
"""
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db, SessionLocal
from ..security import require_role
from ..models import StorageBackend, User
from ..services import storage

router = APIRouter(prefix="/api/storage", tags=["storage"])

BACKEND_TYPES = ("object", "local", "nfs", "block")


def _admin(user: User = Depends(require_role("super_admin", "tenant_admin"))):
    return user


def _row(b: StorageBackend) -> dict:
    return {
        "id": b.id, "name": b.name, "type": b.type, "config": b.config or {},
        "status": b.status, "detail": b.detail,
        "size_used": b.size_used or 0, "size_total": b.size_total or 0,
        "created_at": str(b.created_at),
    }


@router.get("")
def overview(user=Depends(_admin)):
    """主对象存储（rustfs）状态：健康 + 磁盘占用 + 桶统计"""
    h = storage.health()
    try:
        du = shutil.disk_usage(os.path.abspath(settings.data_dir))
        h["disk_used"] = du.used
        h["disk_total"] = du.total
        h["disk_free"] = du.free
    except Exception:
        pass
    try:
        h.update(storage.stats())
    except Exception as e:
        h["stats_error"] = str(e)[:200]
    return h


def _get_backend_or_404(bid: int, db: Session) -> StorageBackend:
    b = db.get(StorageBackend, bid)
    if not b:
        raise HTTPException(404, "存储后端不存在")
    return b


@router.get("/objects")
def objects(prefix: str = "", limit: int = 200, backend: int = 0, user=Depends(_admin)):
    """对象浏览（目录式）；backend>0 时浏览指定对象存储后端"""
    if backend:
        db = SessionLocal()
        try:
            b = _get_backend_or_404(backend, db)
            if b.type != "object":
                raise HTTPException(400, "仅对象存储类型可浏览")
            try:
                items = storage.list_objects_cfg(b.config or {}, prefix, limit)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(502, f"对象存储不可用: {str(e)[:200]}")
        finally:
            db.close()
        return {"prefix": prefix, "items": items, "disabled": False}
    if not storage.enabled():
        return {"prefix": prefix, "items": [], "disabled": True}
    try:
        items = storage.list_objects(prefix, limit)
    except Exception as e:
        raise HTTPException(502, f"对象存储不可用: {str(e)[:200]}")
    return {"prefix": prefix, "items": items, "disabled": False}


@router.delete("/objects")
def delete_objects(prefix: str, backend: int = 0, user=Depends(_admin)):
    """按前缀删除对象（空 prefix 一律拒绝，防误删全桶）"""
    if not prefix or prefix.endswith("/") and len(prefix) <= 1:
        raise HTTPException(400, "prefix 不能为空（不允许删除整个桶）")
    if backend:
        db = SessionLocal()
        try:
            b = _get_backend_or_404(backend, db)
            if b.type != "object":
                raise HTTPException(400, "仅对象存储类型可删除")
            try:
                n = storage.delete_prefix_cfg(b.config or {}, prefix)
            except HTTPException:
                raise
            except Exception as e:
                raise HTTPException(502, f"删除失败: {str(e)[:200]}")
        finally:
            db.close()
        return {"ok": True, "deleted": n}
    if not storage.enabled():
        raise HTTPException(400, "对象存储未启用")
    try:
        n = storage.delete_prefix(prefix)
    except Exception as e:
        raise HTTPException(502, f"删除失败: {str(e)[:200]}")
    return {"ok": True, "deleted": n}


# ---------------------------------------------------------------------------
# 存储后端注册表（多类型对接）
# ---------------------------------------------------------------------------

class BackendReq(BaseModel):
    name: str
    type: str  # object/local/nfs/block
    config: dict = {}


def _validate(type_: str, cfg: dict) -> None:
    if type_ == "object":
        for k in ("endpoint", "bucket"):
            if not (cfg or {}).get(k):
                raise HTTPException(400, f"对象存储必须提供 {k}")
    elif type_ in ("local", "nfs", "block"):
        if not (cfg or {}).get("path"):
            raise HTTPException(400, f"{type_} 必须提供 path（宿主机上的目录）")
        if type_ == "nfs" and not cfg.get("server"):
            raise HTTPException(400, "NFS 必须提供 server（如 192.168.1.10:/export）")
        if type_ == "block" and not cfg.get("device"):
            raise HTTPException(400, "块存储必须提供 device（如 /dev/vdb）")


@router.get("/backends")
def list_backends(db: Session = Depends(get_db), user=Depends(_admin)):
    rows = db.query(StorageBackend).order_by(StorageBackend.id.asc()).all()
    return [_row(b) for b in rows]


@router.post("/backends")
def add_backend(req: BackendReq, db: Session = Depends(get_db), user=Depends(_admin)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "name 必填")
    if req.type not in BACKEND_TYPES:
        raise HTTPException(400, f"type 需为 {'/'.join(BACKEND_TYPES)}")
    if db.query(StorageBackend).filter(StorageBackend.name == name).first():
        raise HTTPException(400, f"名称 {name} 已存在")
    _validate(req.type, req.config)
    r = storage.test_backend(req.type, req.config)
    b = StorageBackend(name=name, type=req.type, config=req.config or {},
                       status="ok" if r["ok"] else "error", detail=r.get("detail", ""),
                       size_used=r.get("size_used", 0), size_total=r.get("size_total", 0))
    db.add(b)
    db.commit()
    db.refresh(b)
    return _row(b)


@router.post("/backends/{bid}/test")
def test_backend(bid: int, db: Session = Depends(get_db), user=Depends(_admin)):
    b = _get_backend_or_404(bid, db)
    r = storage.test_backend(b.type, b.config or {})
    b.status = "ok" if r["ok"] else "error"
    b.detail = r.get("detail", "")
    b.size_used = r.get("size_used", 0)
    b.size_total = r.get("size_total", 0)
    db.commit()
    db.refresh(b)
    return _row(b)


@router.delete("/backends/{bid}")
def delete_backend(bid: int, db: Session = Depends(get_db), user=Depends(_admin)):
    b = _get_backend_or_404(bid, db)
    db.delete(b)
    db.commit()
    return {"ok": True}

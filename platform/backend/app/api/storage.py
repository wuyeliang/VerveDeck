"""存储管理：多类型存储后端注册（对象/本地/NFS/块存储/JuiceFS）+ 对象浏览/删除

对象存储同步队列已整体移除（前端 UI、API 端点、StorageQueue 模型、同步留痕均不再保留）。
object/local 类型真实可用；juicefs = FUSE 挂载点（探测校验 fstype 必须为 fuse）；
nfs/block 为注册 + 状态占位（探测宿主挂载点路径）。
数据集/模型创建时可选注册后端作为存储位置（path 型落挂载点、object 型同步该桶）。
"""
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db, SessionLocal
from ..security import get_current_user, require_role
from ..models import StorageBackend, User
from ..services import storage

router = APIRouter(prefix="/api/storage", tags=["storage"])

BACKEND_TYPES = ("object", "local", "nfs", "block", "juicefs")


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


class BatchObjectsReq(BaseModel):
    names: list[str]
    backend: int = 0


@router.post("/objects/batch-delete")
def batch_delete_objects(req: BatchObjectsReq, user=Depends(_admin)):
    """批量删除对象：逐个执行前缀删除（含空 prefix 防误删校验），返回部分结果"""
    deleted: list[str] = []
    failed: list[dict] = []
    for p in req.names:
        try:
            delete_objects(p, req.backend, user=user)
            deleted.append(p)
        except HTTPException as e:
            failed.append({"name": p, "error": str(e.detail)})
    return {"deleted": deleted, "failed": failed}


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
    type: str  # object/local/nfs/block/juicefs
    config: dict = {}


def _validate(type_: str, cfg: dict) -> None:
    if type_ == "object":
        for k in ("endpoint", "bucket"):
            if not (cfg or {}).get(k):
                raise HTTPException(400, f"对象存储必须提供 {k}")
    elif type_ == "juicefs":
        # config: {path(挂载点,必填,如 /data/juicefs), meta_url(可选,展示), volume(可选,展示)}
        p = (cfg or {}).get("path", "")
        if not p:
            raise HTTPException(400, "JuiceFS 必须提供 path（宿主 FUSE 挂载点，如 /data/juicefs）")
        if not p.startswith("/"):
            raise HTTPException(400, "JuiceFS 挂载点 path 需为绝对路径")
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


@router.get("/backends/options")
def backend_options(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """数据集/模型创建页的「存储位置」下拉：任意登录用户可读，仅 id/name/type（不含 config 凭证）"""
    rows = db.query(StorageBackend).order_by(StorageBackend.id.asc()).all()
    return [{"id": b.id, "name": b.name, "type": b.type, "status": b.status} for b in rows]


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
    # 引用检查：被数据集/模型选为存储位置的后端不可删除（避免存量文件"失联"）
    from ..models import Dataset, ModelAsset
    n_ds = db.query(Dataset).filter(Dataset.storage_backend_id == bid).count()
    n_md = db.query(ModelAsset).filter(ModelAsset.storage_backend_id == bid).count()
    if n_ds or n_md:
        raise HTTPException(400, f"后端被 {n_ds} 个数据集 / {n_md} 个模型引用，不可删除")
    db.delete(b)
    db.commit()
    return {"ok": True}

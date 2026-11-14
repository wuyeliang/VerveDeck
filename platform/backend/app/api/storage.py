"""对象存储管理：状态/浏览/删除 + 同步队列（留痕 + 重试）"""
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db, SessionLocal
from ..security import get_current_user, require_role
from ..models import StorageQueue, User
from ..services import storage

router = APIRouter(prefix="/api/storage", tags=["storage"])


def _admin(user: User = Depends(require_role("super_admin", "tenant_admin"))):
    return user


@router.get("")
def overview(user=Depends(_admin)):
    """存储状态：S3 健康 + 磁盘占用 + 桶统计"""
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


@router.get("/objects")
def objects(prefix: str = "", limit: int = 200, user=Depends(_admin)):
    """对象浏览（目录式：文件夹 + 文件）"""
    if not storage.enabled():
        return {"prefix": prefix, "items": [], "disabled": True}
    try:
        items = storage.list_objects(prefix, limit)
    except Exception as e:
        raise HTTPException(502, f"对象存储不可用: {str(e)[:200]}")
    return {"prefix": prefix, "items": items, "disabled": False}


@router.delete("/objects")
def delete_objects(prefix: str, user=Depends(_admin)):
    """按前缀删除对象（空 prefix 一律拒绝，防误删全桶）"""
    if not prefix or prefix.endswith("/") and len(prefix) <= 1:
        raise HTTPException(400, "prefix 不能为空（不允许删除整个桶）")
    if not storage.enabled():
        raise HTTPException(400, "对象存储未启用")
    try:
        n = storage.delete_prefix(prefix)
    except Exception as e:
        raise HTTPException(502, f"删除失败: {str(e)[:200]}")
    return {"ok": True, "deleted": n}


@router.get("/queue")
def queue(status: str = "", limit: int = 100, db: Session = Depends(get_db), user=Depends(_admin)):
    """同步队列：S3 上传/下载/删除留痕"""
    q = db.query(StorageQueue)
    if status:
        q = q.filter(StorageQueue.status == status)
    total = q.count()
    rows = (q.order_by(StorageQueue.id.desc()).limit(min(max(limit, 1), 500)).all())
    return {"total": total, "items": [
        {"id": r.id, "op": r.op, "key": r.key, "status": r.status,
         "error": r.error, "created_at": str(r.created_at)} for r in rows]}


@router.post("/queue/{qid}/retry")
def retry(qid: int, user=Depends(_admin)):
    """重试失败的同步操作（仅 put：源文件仍在本地时重新上传；delete 重新删前缀）"""
    db = SessionLocal()
    try:
        r = db.get(StorageQueue, qid)
        if not r:
            raise HTTPException(404, "记录不存在")
        if r.status != "failed":
            raise HTTPException(400, f"仅 failed 记录可重试（当前 {r.status}）")
        if r.op == "put" and os.path.exists(r.local_path):
            ok = storage.put_file(r.local_path)
            r.status = "done" if ok else "failed"
            r.error = "" if ok else r.error
            db.commit()
            return {"ok": ok}
        if r.op == "delete" and r.key:
            try:
                n = storage.delete_prefix(r.key)
                r.status = "done"
                r.error = f"{n} objects (retry)"
                db.commit()
                return {"ok": True, "deleted": n}
            except Exception as e:
                r.error = str(e)[:1000]
                db.commit()
                raise HTTPException(502, f"重试失败: {str(e)[:200]}")
        raise HTTPException(400, f"该记录无法自动重试（op={r.op}）")
    except HTTPException:
        raise
    finally:
        db.close()


@router.delete("/queue")
def clear_queue(status: str = "done", db: Session = Depends(get_db), user=Depends(_admin)):
    """清理队列记录（默认清理 done；可传 failed）"""
    if status not in ("done", "failed"):
        raise HTTPException(400, "status 仅支持 done/failed")
    n = (db.query(StorageQueue).filter(StorageQueue.status == status)
         .delete(synchronize_session=False))
    db.commit()
    return {"ok": True, "deleted": n}

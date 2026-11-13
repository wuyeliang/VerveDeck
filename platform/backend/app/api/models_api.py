"""模型资产仓库：基座模型纳管（HF 下载）+ 微调产物归档 + 血缘追踪"""
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..security import get_current_user, can_access_project, require_role
from ..models import ModelAsset, Project, TrainingJob, DatasetVersion, User

router = APIRouter(prefix="/api/models", tags=["models"])


def _check_project(db, pid: int, user: User):
    p = db.get(Project, pid)
    if not p or not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")
    return p


def _brief(a: ModelAsset, db: Session) -> dict:
    base = db.get(ModelAsset, a.parent_asset_id) if a.parent_asset_id else None
    return {
        "id": a.id, "project_id": a.project_id, "name": a.name, "type": a.type,
        "path": a.path, "source": a.source, "hf_id": a.hf_id,
        "size_bytes": a.size_bytes, "status": a.status,
        "parent_name": base.name if base else "",
        "job_id": a.job_id, "dataset_version_ids": a.dataset_version_ids or [],
        "note": a.note, "created_at": str(a.created_at),
    }


def _size(path: str) -> int:
    if os.path.isfile(path):  # 单文件路径（os.walk 对文件返回 0）
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


@router.get("")
def list_models(project_id: int | None = None, mtype: str = "", db=Depends(get_db),
                user=Depends(get_current_user)):
    if project_id:
        _check_project(db, project_id, user)
        q = db.query(ModelAsset).filter(ModelAsset.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = db.query(ModelAsset).filter(ModelAsset.project_id.in_(pids or [0]))
    if mtype:
        q = q.filter(ModelAsset.type == mtype)
    rows = q.order_by(ModelAsset.id.desc()).all()
    return [_brief(a, db) for a in rows]


class BaseReq(BaseModel):
    project_id: int
    name: str
    hf_id: str  # 如 Qwen/Qwen2.5-0.5B-Instruct
    note: str = ""


@router.post("/base")
def register_base(req: BaseReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check_project(db, req.project_id, user)
    if db.query(ModelAsset).filter_by(project_id=req.project_id, name=req.name).first():
        raise HTTPException(400, "同名模型已存在")
    path = os.path.join(settings.data_dir, "models", "hub", req.name)
    a = ModelAsset(project_id=req.project_id, name=req.name, type="base", path=path,
                   source=f"hf:{req.hf_id}", hf_id=req.hf_id,
                   status="pending_download" if not os.path.exists(path) else "ready",
                   note=req.note, created_by=user.id)
    if os.path.exists(path):
        a.size_bytes = _size(path)
        a.status = "ready"
    db.add(a)
    db.commit()
    db.refresh(a)
    return _brief(a, db)


class ArtifactReq(BaseModel):
    project_id: int
    name: str
    type: str  # lora/merged
    path: str
    base_model_name: str = ""
    note: str = ""


@router.post("")
def register_artifact(req: ArtifactReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check_project(db, req.project_id, user)
    if req.type not in ("lora", "merged"):
        raise HTTPException(400, "type 需为 lora/merged")
    if not os.path.exists(req.path):
        raise HTTPException(400, f"路径不存在: {req.path}")
    if db.query(ModelAsset).filter_by(project_id=req.project_id, name=req.name).first():
        raise HTTPException(400, "同名模型已存在")
    parent = None
    if req.base_model_name:
        parent = db.query(ModelAsset).filter_by(project_id=req.project_id, name=req.base_model_name).first()
    a = ModelAsset(project_id=req.project_id, name=req.name, type=req.type, path=req.path,
                   source=req.base_model_name, parent_asset_id=parent.id if parent else None,
                   note=req.note, created_by=user.id)
    a.size_bytes = _size(req.path)
    db.add(a)
    db.commit()
    db.refresh(a)
    return _brief(a, db)


@router.get("/{aid}")
def get_model(aid: int, db=Depends(get_db), user=Depends(get_current_user)):
    a = db.get(ModelAsset, aid)
    if not a:
        raise HTTPException(404, "模型不存在")
    _check_project(db, a.project_id, user)
    return _brief(a, db)


@router.get("/{aid}/lineage")
def lineage(aid: int, db=Depends(get_db), user=Depends(get_current_user)):
    """血缘：父模型链 + 训练任务 + 数据集版本 + 由它产出的子模型"""
    a = db.get(ModelAsset, aid)
    if not a:
        raise HTTPException(404, "模型不存在")
    _check_project(db, a.project_id, user)
    chain = []
    cur = a
    seen = set()
    while cur and cur.id not in seen:
        seen.add(cur.id)
        chain.append({"id": cur.id, "name": cur.name, "type": cur.type, "status": cur.status})
        cur = db.get(ModelAsset, cur.parent_asset_id) if cur.parent_asset_id else None
    jobs = db.query(TrainingJob).filter(TrainingJob.id.in_(
        [j.id for j in db.query(TrainingJob).filter(TrainingJob.base_model_id == aid).all()])).all() if a else []
    # 该模型作为 base 派生出的模型
    children = db.query(ModelAsset).filter(ModelAsset.parent_asset_id == aid).all()
    # 训练任务（以其为 base 的）
    train_jobs = db.query(TrainingJob).filter(TrainingJob.base_model_id == aid).limit(20).all()
    dsvs = []
    for vid in (a.dataset_version_ids or []):
        v = db.get(DatasetVersion, vid)
        if v:
            dsvs.append({"id": v.id, "version": v.version, "note": v.note})
    return {
        "asset": _brief(a, db),
        "chain": chain,
        "children": [_brief(c, db) for c in children],
        "training_jobs": [{"id": j.id, "name": j.name, "status": j.status,
                           "stage": j.stage, "created_at": str(j.created_at)} for j in train_jobs],
        "dataset_versions": dsvs,
    }


@router.delete("/{aid}")
def delete_model(aid: int, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin", "developer"))):
    a = db.get(ModelAsset, aid)
    if not a:
        raise HTTPException(404, "模型不存在")
    _check_project(db, a.project_id, user)
    db.delete(a)
    db.commit()
    return {"ok": True, "note": "仅删除仓库登记，磁盘文件未删除（如需释放磁盘请手动 rm）"}

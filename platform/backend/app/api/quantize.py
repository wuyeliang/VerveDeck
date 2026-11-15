"""模型压缩（量化）中心：参照百炼——仅支持量化；任务排队 + 调度器启动容器执行"""
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..database import get_db
from ..security import get_current_user, can_access_project
from ..models import QuantTask, Project, ModelAsset
from ..services import quantize_service as qs
from ..scheduler import _tail

router = APIRouter(prefix="/api/quantize", tags=["quantize"])


def _check(db, pid, user):
    p = db.get(Project, pid)
    if not p or not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")


def _brief(t: QuantTask, db) -> dict:
    asset = db.get(ModelAsset, t.model_asset_id) if t.model_asset_id else None
    return {
        "id": t.id, "project_id": t.project_id, "name": t.name,
        "model_asset_id": t.model_asset_id,
        "model_name": asset.name if asset else "",
        "method": t.method,
        "method_label": qs.METHODS.get(t.method, {}).get("label", t.method),
        "gpu_count": t.gpu_count,
        "status": t.status, "error": t.error or "",
        "created_at": str(t.created_at),
        "finished_at": str(t.finished_at) if t.finished_at else "",
    }


class QuantReq(BaseModel):
    project_id: int
    name: str
    model_asset_id: int
    method: str  # bnb4/bnb8/gptq4/awq4


@router.get("/methods")
def methods(user=Depends(get_current_user)):
    out = []
    for k, m in qs.METHODS.items():
        out.append({"key": k, "bit": m["bit"], "method": m["method"],
                    "device": m["device"], "label": m["label"]})
    return out


@router.get("")
def list_tasks(project_id: int | None = None, db=Depends(get_db),
               user=Depends(get_current_user)):
    if project_id:
        _check(db, project_id, user)
        q = db.query(QuantTask).filter(QuantTask.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = db.query(QuantTask).filter(QuantTask.project_id.in_(pids or [0]))
    rows = q.order_by(QuantTask.id.desc()).limit(200).all()
    return [_brief(t, db) for t in rows]


@router.post("")
def create_task(req: QuantReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check(db, req.project_id, user)
    if req.method not in qs.METHODS:
        raise HTTPException(400, f"method 需为 {list(qs.METHODS)} 之一")
    asset = db.get(ModelAsset, req.model_asset_id)
    if not asset or asset.project_id != req.project_id:
        raise HTTPException(404, "模型不存在或不属于该项目")
    if asset.status != "ready":
        raise HTTPException(400, f"模型尚未就绪（当前状态 {asset.status}），下载/训练完成后再量化")
    if asset.type == "lora":
        raise HTTPException(400, "LoRA 权重需先合并为完整模型再量化（训练任务选 merge 模式产出 merged）")
    busy = db.query(QuantTask).filter(
        QuantTask.model_asset_id == asset.id,
        QuantTask.status.in_(["pending", "running"])).first()
    if busy:
        raise HTTPException(400, f"模型已有进行中的压缩任务 #{busy.id}（{busy.status}）")
    t = QuantTask(project_id=req.project_id, name=req.name,
                  model_asset_id=asset.id, method=req.method,
                  gpu_count=1, status="pending", created_by=user.id)
    db.add(t)
    db.commit()
    db.refresh(t)
    return _brief(t, db)


@router.get("/{tid}")
def get_task(tid: int, db=Depends(get_db), user=Depends(get_current_user)):
    t = db.get(QuantTask, tid)
    if not t:
        raise HTTPException(404, "压缩任务不存在")
    _check(db, t.project_id, user)
    return _brief(t, db)


@router.get("/{tid}/logs")
def task_logs(tid: int, tail: int = 120, db=Depends(get_db), user=Depends(get_current_user)):
    t = db.get(QuantTask, tid)
    if not t:
        raise HTTPException(404, "压缩任务不存在")
    _check(db, t.project_id, user)
    return {"logs": _tail(os.path.join(qs.task_dir(tid), "run.log"), tail),
            "status": t.status}


@router.delete("/{tid}")
def delete_task(tid: int, db=Depends(get_db), user=Depends(get_current_user)):
    t = db.get(QuantTask, tid)
    if not t:
        raise HTTPException(404, "压缩任务不存在")
    _check(db, t.project_id, user)
    if t.status in ("pending", "running"):
        raise HTTPException(400, "进行中（排队/运行）的压缩任务不可删除，请等待完成或节点空闲后重试")
    # 产物保护：成功任务的量化产物可能已注册进模型仓库（path == output_dir），
    # 此时删除任务目录会连带 rmtree 量化权重、留下悬空资产 → 拒绝删除
    # （与训练任务"已产出模型资产不可删"的约定一致）
    out = t.output_dir or f"{qs.task_dir(tid)}/out"
    if db.query(ModelAsset).filter(ModelAsset.path == out).first():
        raise HTTPException(400, "任务已产出模型资产，请先在模型仓库删除对应产物再删除任务")
    db.delete(t)
    db.commit()
    shutil.rmtree(qs.task_dir(tid), ignore_errors=True)
    return {"ok": True}


class BatchDeleteReq(BaseModel):
    ids: list[int]


@router.post("/batch-delete")
def batch_delete_tasks(req: BatchDeleteReq, db=Depends(get_db), user=Depends(get_current_user)):
    """批量删除：逐个执行单删逻辑（进行中的任务跳过并报错），返回部分结果"""
    deleted: list[int] = []
    failed: list[dict] = []
    for tid in req.ids:
        try:
            delete_task(tid, db=db, user=user)
            deleted.append(tid)
        except HTTPException as e:
            db.rollback()
            failed.append({"id": tid, "error": str(e.detail)})
    return {"deleted": deleted, "failed": failed}

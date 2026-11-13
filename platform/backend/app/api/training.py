"""训练中心：CPT/SFT/DPO/合并 全链路，LoRA/DeepSpeed，实时 Loss 监控，断点续训"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..security import get_current_user, can_access_project
from ..models import TrainingJob, ModelAsset, DatasetVersion, Project, User
from ..tfevents import read_metrics
from ..services import training_service as ts

router = APIRouter(prefix="/api/training", tags=["training"])


def _check(db, pid, user):
    p = db.get(Project, pid)
    if not p or not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")
    return p


def _brief(j: TrainingJob, db: Session) -> dict:
    base = db.get(ModelAsset, j.base_model_id) if j.base_model_id else None
    return {
        "id": j.id, "project_id": j.project_id, "name": j.name, "stage": j.stage,
        "finetuning_type": j.finetuning_type, "base_model": base.name if base else "",
        "dataset_version_ids": j.dataset_version_ids or [],
        "gpu_count": j.gpu_count, "priority": j.priority, "deepspeed": j.deepspeed,
        "status": j.status, "hyperparams": j.hyperparams or {},
        "resume_from_job_id": j.resume_from_job_id,
        "error": j.error, "work_dir": j.work_dir,
        "created_at": str(j.created_at), "started_at": str(j.started_at) if j.started_at else "",
        "finished_at": str(j.finished_at) if j.finished_at else "",
    }


class JobReq(BaseModel):
    project_id: int
    name: str
    stage: str = "sft"  # cpt/sft/dpo/export
    finetuning_type: str = "lora"  # lora/full
    base_model_id: int | None = None
    adapter_asset_id: int | None = None  # export 用
    dataset_version_ids: list[int] = []
    hyperparams: dict = {}
    deepspeed: str = "none"  # none/zero2/zero3
    gpu_count: int = 1
    priority: int = 5


@router.post("/jobs")
def create_job(req: JobReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check(db, req.project_id, user)
    if req.stage not in ("cpt", "sft", "dpo", "export"):
        raise HTTPException(400, "stage 需为 cpt/sft/dpo/export")
    if req.stage != "export" and not req.dataset_version_ids:
        raise HTTPException(400, "需指定训练数据集版本")
    if not req.base_model_id:
        raise HTTPException(400, "需指定基座模型")
    base = db.get(ModelAsset, req.base_model_id)
    if not base:
        raise HTTPException(404, "基座模型不存在")
    if base.status != "ready":
        raise HTTPException(400, f"基座模型未就绪(status={base.status})，请等待下载完成")
    if req.stage == "export" and not req.adapter_asset_id:
        raise HTTPException(400, "export 需指定 LoRA 产物 (adapter_asset_id)")
    job = TrainingJob(
        project_id=req.project_id, name=req.name, stage=req.stage,
        finetuning_type=req.finetuning_type if req.stage != "export" else "lora",
        base_model_id=req.base_model_id, adapter_asset_id=req.adapter_asset_id,
        dataset_version_ids=req.dataset_version_ids, hyperparams=req.hyperparams,
        deepspeed=req.deepspeed, gpu_count=req.gpu_count, priority=req.priority,
        status="queued", submitted_by=user.id,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    job.work_dir = ts.job_dir(job.id)
    db.commit()
    return _brief(job, db)


@router.get("/jobs")
def list_jobs(project_id: int | None = None, status: str = "", db=Depends(get_db),
              user=Depends(get_current_user)):
    if project_id:
        _check(db, project_id, user)
        q = db.query(TrainingJob).filter(TrainingJob.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = db.query(TrainingJob).filter(TrainingJob.project_id.in_(pids or [0]))
    if status:
        q = q.filter(TrainingJob.status == status)
    rows = q.order_by(TrainingJob.id.desc()).limit(200).all()
    return [_brief(j, db) for j in rows]


@router.get("/jobs/{jid}")
def get_job(jid: int, db=Depends(get_db), user=Depends(get_current_user)):
    j = db.get(TrainingJob, jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    _check(db, j.project_id, user)
    try:
        config = ts.build_config(j, db)  # 最终生效的 LLaMA-Factory 配置（spec 默认+用户覆盖+派生）
    except Exception:
        config = {}
    return {**_brief(j, db),
            "checkpoints": [{"step": c["step"], "path": c["path"]} for c in ts.checkpoints(j)],
            "config": config,
            "container_name": j.container_name}


@router.get("/jobs/{jid}/metrics")
def job_metrics(jid: int, db=Depends(get_db), user=Depends(get_current_user)):
    j = db.get(TrainingJob, jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    _check(db, j.project_id, user)
    return read_metrics(j.tensorboard_dir)


@router.get("/jobs/{jid}/logs")
def job_logs(jid: int, tail: int = 200, db=Depends(get_db), user=Depends(get_current_user)):
    j = db.get(TrainingJob, jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    _check(db, j.project_id, user)
    import os
    p = f"{ts.job_dir(jid)}/run.log"
    if not os.path.exists(p):
        return {"logs": ""}
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 60000))
        data = f.read().decode("utf-8", "ignore")
    return {"logs": "\n".join(data.splitlines()[-tail:])}


@router.post("/jobs/{jid}/cancel")
def cancel_job(jid: int, db=Depends(get_db), user=Depends(get_current_user)):
    j = db.get(TrainingJob, jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    _check(db, j.project_id, user)
    if j.status not in ("queued", "running"):
        raise HTTPException(400, f"当前状态 {j.status} 不可取消")
    if j.status == "running":
        ts.cancel(j)
    j.status = "cancelled"
    from datetime import datetime
    j.finished_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@router.delete("/jobs/{jid}")
def delete_job(jid: int, db=Depends(get_db), user=Depends(get_current_user)):
    j = db.get(TrainingJob, jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    _check(db, j.project_id, user)
    if j.status in ("queued", "running"):
        raise HTTPException(400, f"当前状态 {j.status} 不可删除（请先取消）")
    if db.query(ModelAsset).filter(ModelAsset.job_id == jid).first():
        raise HTTPException(400, "任务已产出模型资产，请先在模型仓库删除对应产物再删除任务")
    if db.query(TrainingJob).filter(TrainingJob.resume_from_job_id == jid).first():
        raise HTTPException(400, "存在基于该任务续训的新任务，不可删除")
    work_dir = j.work_dir
    db.delete(j)
    db.commit()
    # 磁盘清理：仅限平台数据目录内的任务工作目录
    import os
    try:
        root = os.path.realpath(settings.data_dir)
        rp = os.path.realpath(work_dir)
        if rp.startswith(root + os.sep) and os.path.isdir(rp):
            import shutil
            shutil.rmtree(rp, ignore_errors=True)
    except Exception:
        pass
    return {"ok": True}


class ResumeReq(BaseModel):
    checkpoint: str = ""  # 空=取最新


@router.post("/jobs/{jid}/resume")
def resume_job(jid: int, req: ResumeReq, db=Depends(get_db), user=Depends(get_current_user)):
    j = db.get(TrainingJob, jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    _check(db, j.project_id, user)
    ckpts = ts.checkpoints(j)
    if not ckpts:
        raise HTTPException(400, "无可用的 checkpoint")
    ckpt = req.checkpoint or ckpts[0]["path"]
    new = TrainingJob(
        project_id=j.project_id, name=f"{j.name}-resume", stage=j.stage,
        finetuning_type=j.finetuning_type, base_model_id=j.base_model_id,
        dataset_version_ids=j.dataset_version_ids,
        hyperparams={**(j.hyperparams or {}), "resume_from_checkpoint": ckpt},
        deepspeed=j.deepspeed, gpu_count=j.gpu_count, priority=j.priority,
        status="queued", submitted_by=user.id, resume_from_job_id=j.id,
    )
    db.add(new)
    db.commit()
    db.refresh(new)
    return _brief(new, db)


@router.get("/params")
def param_spec():
    """LLaMA-Factory 全量参数面（分组），前端超参向导据此渲染动态表单"""
    return ts.param_spec()


@router.get("/templates")
def templates():
    """各阶段超参模板（前端向导默认值）"""
    return {
        "cpt": {"template": "qwen", "cutoff_len": 4096, "per_device_train_batch_size": 1,
                "gradient_accumulation_steps": 8, "learning_rate": 2e-5, "num_train_epochs": 1.0,
                "save_steps": 500, "logging_steps": 10},
        "sft": {"template": "qwen", "cutoff_len": 2048, "per_device_train_batch_size": 2,
                "gradient_accumulation_steps": 8, "learning_rate": 1e-4, "num_train_epochs": 3.0,
                "save_steps": 200, "logging_steps": 5, "lora_rank": 8, "lora_alpha": 32},
        "dpo": {"template": "qwen", "cutoff_len": 2048, "per_device_train_batch_size": 1,
                "gradient_accumulation_steps": 8, "learning_rate": 5e-6, "num_train_epochs": 1.0,
                "save_steps": 200, "logging_steps": 5, "lora_rank": 8, "lora_alpha": 32},
        "export": {"export_size": "2", "export_device": "cpu"},
    }

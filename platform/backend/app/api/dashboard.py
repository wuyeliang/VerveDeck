"""Dashboard 汇总"""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..database import get_db
from ..security import get_current_user
from ..models import (Dataset, DatasetVersion, ModelAsset, TrainingJob, EvalTask,
                      Deployment, GpuNode, GpuMetric, AuditLog, User, Project)
from .. import scheduler

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


@router.get("/summary")
def summary(db=Depends(get_db), user=Depends(get_current_user)):
    from ..security import can_access_project
    pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]

    node = db.query(GpuNode).filter_by(name="node-1").first()
    last = db.query(GpuMetric).order_by(GpuMetric.ts.desc()).first()
    util = last.util if last else 0
    mem_used = last.mem_used if last else 0
    mem_total = last.mem_total if last else 0

    return {
        "datasets": db.query(Dataset).filter(Dataset.project_id.in_(pids or [0])).count(),
        "dataset_versions": db.query(DatasetVersion).join(Dataset).filter(Dataset.project_id.in_(pids or [0])).count(),
        "models": db.query(ModelAsset).filter(ModelAsset.project_id.in_(pids or [0])).count(),
        "jobs_total": db.query(TrainingJob).filter(TrainingJob.project_id.in_(pids or [0])).count(),
        "jobs_running": db.query(TrainingJob).filter(TrainingJob.status == "running").count(),
        "jobs_failed": db.query(TrainingJob).filter(TrainingJob.status == "failed").count(),
        "evals": db.query(EvalTask).filter(EvalTask.project_id.in_(pids or [0])).count(),
        "deployments_running": db.query(Deployment).filter(Deployment.status == "running").count(),
        "gpu": {
            "node": node.name if node else "",
            "status": node.status if node else "unknown",
            "count": node.gpu_count if node else 0,
            "util": util, "mem_used": mem_used, "mem_total": mem_total,
        },
        "recent_jobs": [
            {"id": j.id, "name": j.name, "stage": j.stage, "status": j.status,
             "created_at": str(j.created_at)}
            for j in db.query(TrainingJob).order_by(TrainingJob.id.desc()).limit(8).all()
        ],
        # 对象存储状态（rustfs/S3；未配置时 ok=True 表示纯本地模式）
        "storage": _storage_health(),
    }


def _storage_health() -> dict:
    from ..services import storage
    try:
        return storage.health()
    except Exception as e:  # 防御：健康检查本身不允许 500
        return {"enabled": False, "endpoint": "", "bucket": "", "ok": False, "detail": str(e)[:200]}

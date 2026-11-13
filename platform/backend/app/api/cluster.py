"""算力调度与运维：GPU 节点池、利用率监控、多任务排队与优先级、故障自愈视图"""
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from ..config import settings
from ..database import get_db
from ..security import get_current_user
from ..models import (GpuNode, GpuMetric, TrainingJob, Deployment, User, Project)
from .. import scheduler

router = APIRouter(prefix="/api/cluster", tags=["cluster"])


@router.get("/nodes")
def nodes(db=Depends(get_db), user=Depends(get_current_user)):
    out = []
    for n in db.query(GpuNode).all():
        last = (db.query(GpuMetric)
                .filter(GpuMetric.node_id == n.id)
                .order_by(GpuMetric.ts.desc()).first())
        out.append({
            "id": n.id, "name": n.name, "gpu_count": n.gpu_count, "status": n.status,
            "last_heartbeat": str(n.last_heartbeat) if n.last_heartbeat else "",
            "meta": n.meta,
            "gpus": ([{"index": last.gpu_index, "mem_used": last.mem_used,
                       "mem_total": last.mem_total, "util": last.util,
                       "power": last.power, "temp": last.temp}] if last else []),
            "gpu_in_use": scheduler._gpu_in_use(db),
        })
    return out


@router.get("/nodes/{nid}/metrics")
def node_metrics(nid: int, minutes: int = 60, db=Depends(get_db), user=Depends(get_current_user)):
    n = db.get(GpuNode, nid)
    if not n:
        raise HTTPException(404, "节点不存在")
    since = int(time.time()) - minutes * 60
    rows = (db.query(GpuMetric)
            .filter(GpuMetric.node_id == nid, GpuMetric.ts >= since)
            .order_by(GpuMetric.ts.asc()).all())
    series = {}
    for r in rows:
        series.setdefault(r.gpu_index, []).append({
            "ts": r.ts, "util": r.util, "mem_used": r.mem_used,
            "power": r.power, "temp": r.temp,
        })
    return {"node": n.name, "series": series}


class PriorityReq(BaseModel):
    priority: int  # 1-9


@router.get("/queue")
def queue(db=Depends(get_db), user=Depends(get_current_user)):
    jobs = (db.query(TrainingJob)
            .filter(TrainingJob.status.in_(["queued"]))
            .order_by(TrainingJob.priority.desc(), TrainingJob.id.asc()).all())
    running = (db.query(TrainingJob).filter(TrainingJob.status == "running").all())
    return {
        "queued": [{"id": j.id, "name": j.name, "stage": j.stage, "gpu_count": j.gpu_count,
                    "priority": j.priority, "created_at": str(j.created_at)} for j in jobs],
        "running": [{"id": j.id, "name": j.name, "stage": j.stage, "gpu_count": j.gpu_count,
                     "started_at": str(j.started_at) if j.started_at else ""} for j in running],
    }


@router.put("/queue/{jid}")
def set_priority(jid: int, req: PriorityReq, db=Depends(get_db), user=Depends(get_current_user)):
    j = db.get(TrainingJob, jid)
    if not j:
        raise HTTPException(404, "任务不存在")
    if j.status != "queued":
        raise HTTPException(400, "仅排队中任务可调优先级")
    j.priority = max(1, min(9, req.priority))
    db.commit()
    return {"ok": True, "priority": j.priority}


@router.post("/queue/{jid}/promote")
def promote(jid: int, db=Depends(get_db), user=Depends(get_current_user)):
    j = db.get(TrainingJob, jid)
    if not j or j.status != "queued":
        raise HTTPException(404, "任务不存在或不在排队中")
    j.priority = 9
    db.commit()
    return {"ok": True}


@router.get("/utilization")
def utilization(db=Depends(get_db), user=Depends(get_current_user)):
    n = db.query(GpuNode).filter_by(name=settings.node_name).first()
    used = scheduler._gpu_in_use(db)
    total = n.gpu_count if n else 0
    running_dep = db.query(Deployment).filter(Deployment.status == "running").count()
    running_job = db.query(TrainingJob).filter(TrainingJob.status == "running").count()
    return {"node": n.name if n else "", "total_gpus": total, "used_gpus": used,
            "free_gpus": max(0, total - used), "running_deployments": running_dep,
            "running_jobs": running_job, "node_status": n.status if n else "unknown"}

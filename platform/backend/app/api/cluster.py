"""算力调度与运维：GPU 节点池、利用率监控、多任务排队与优先级、故障自愈视图"""
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from sqlalchemy import func

from ..config import settings
from ..database import get_db
from ..security import get_current_user
from ..models import (GpuNode, GpuMetric, SystemMetric, TrainingJob, Deployment, User, Project)
from .. import scheduler

router = APIRouter(prefix="/api/cluster", tags=["cluster"])


@router.get("/nodes")
def nodes(db=Depends(get_db), user=Depends(get_current_user)):
    out = []
    for n in db.query(GpuNode).all():
        last_ts = (db.query(func.max(GpuMetric.ts))
                   .filter(GpuMetric.node_id == n.id).scalar())
        rows = ((db.query(GpuMetric)
                 .filter(GpuMetric.node_id == n.id, GpuMetric.ts == last_ts)
                 .order_by(GpuMetric.gpu_index.asc()).all())
                if last_ts else [])
        # 防御：同 ts 历史重复行（tick/采集周期相位漂移的旧数据），按 gpu_index 去重
        _seen = set()
        deduped = []
        for r in rows:
            if r.gpu_index in _seen:
                continue
            _seen.add(r.gpu_index)
            deduped.append(r)
        rows = deduped
        meta_names = (n.meta or {}).get("names", []) or []
        out.append({
            "id": n.id, "name": n.name, "gpu_count": n.gpu_count, "status": n.status,
            "last_heartbeat": str(n.last_heartbeat) if n.last_heartbeat else "",
            "meta": n.meta,
            "gpus": ([{"index": r.gpu_index,
                       "name": (meta_names[r.gpu_index]
                                if r.gpu_index < len(meta_names) else ""),
                       "mem_used": r.mem_used, "mem_total": r.mem_total,
                       "util": r.util, "power": r.power, "temp": r.temp,
                       "extra": r.extra or {}} for r in rows]),
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


@router.get("/system")
def system_metrics(minutes: int = 60, db=Depends(get_db), user=Depends(get_current_user)):
    """宿主 Linux 系统指标（CPU/内存/磁盘/负载/网络）：最新快照 + 时间序列（gpu-agent 5s 采集）"""
    node = db.query(GpuNode).filter_by(name=settings.node_name).first()
    if not node:
        return {"node": "", "latest": None, "series": []}
    since = int(time.time()) - minutes * 60
    rows = (db.query(SystemMetric)
            .filter(SystemMetric.node_id == node.id, SystemMetric.ts >= since)
            .order_by(SystemMetric.ts.asc()).all())
    last = rows[-1] if rows else None
    return {
        "node": node.name,
        "latest": (None if not last else {
            "ts": last.ts, "cpu_pct": last.cpu_pct, "cpu_count": last.cpu_count,
            "load1": last.load1, "load5": last.load5, "load15": last.load15,
            "mem_total_mb": last.mem_total_mb, "mem_used_mb": last.mem_used_mb, "mem_pct": last.mem_pct,
            "disk_total_mb": last.disk_total_mb, "disk_used_mb": last.disk_used_mb, "disk_pct": last.disk_pct,
            "net_rx_mbps": last.net_rx_mbps, "net_tx_mbps": last.net_tx_mbps,
        }),
        "series": [{"ts": r.ts, "cpu": r.cpu_pct, "mem": r.mem_pct, "disk": r.disk_pct,
                    "load1": r.load1, "net_rx": r.net_rx_mbps, "net_tx": r.net_tx_mbps}
                   for r in rows],
    }


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


@router.get("/inference")
def inference_overview(db=Depends(get_db), user=Depends(get_current_user)):
    """推理服务总览（监控大屏用）：运行中部署 + 最新引擎指标 + 近 1h KV/请求趋势"""
    from ..models import InferenceMetric
    deps = db.query(Deployment).filter(Deployment.status == "running").all()
    since = int(time.time()) - 3600
    out = []
    for d in deps:
        last = (db.query(InferenceMetric).filter(InferenceMetric.deployment_id == d.id)
                .order_by(InferenceMetric.ts.desc()).first())
        rows = (db.query(InferenceMetric).filter(InferenceMetric.deployment_id == d.id,
                                                 InferenceMetric.ts >= since)
                .order_by(InferenceMetric.ts.asc()).all())
        out.append({
            "id": d.id, "name": d.name, "engine": d.engine,
            "latest": (None if not last else {
                "kv": last.kv_cache_pct, "running": last.num_running,
                "waiting": last.num_waiting, "latency_ms": last.avg_latency_ms}),
            "series": [{"ts": r.ts, "kv": r.kv_cache_pct, "running": r.num_running}
                       for r in rows],
        })
    return {"items": out}

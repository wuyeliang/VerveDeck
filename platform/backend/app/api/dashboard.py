"""Dashboard 汇总"""
import os
import shutil

from fastapi import APIRouter, Depends
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..security import get_current_user
from ..config import settings
from ..models import (Dataset, DatasetVersion, ModelAsset, TrainingJob, EvalTask,
                      Deployment, GpuNode, GpuMetric, AuditLog, User, Project,
                      ApiKey, DataTask)
from .. import scheduler

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])

_THROTTLE_KEYS = ("thermal_slowdown", "hw_slowdown", "hw_thermal_slowdown",
                  "hw_power_brake_slowdown", "sw_thermal_slowdown")


def _status_counts(db, model, pids, statuses):
    """按状态分组计数（限定可访问项目），缺失状态补 0"""
    out = {s: 0 for s in statuses}
    rows = (db.query(model.status, func.count(model.id))
            .filter(model.project_id.in_(pids))
            .group_by(model.status).all())
    for st, n in rows:
        out[st] = out.get(st, 0) + n
    return out


@router.get("/summary")
def summary(db=Depends(get_db), user=Depends(get_current_user)):
    from ..security import can_access_project
    pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
    pids = pids or [0]  # 无项目时查不到任何行

    node = db.query(GpuNode).filter_by(name="node-1").first()
    last = db.query(GpuMetric).order_by(GpuMetric.ts.desc()).first()
    util = last.util if last else 0
    mem_used = last.mem_used if last else 0
    mem_total = last.mem_total if last else 0

    # 各模块状态分布
    jobs = _status_counts(db, TrainingJob, pids,
                          ("queued", "running", "succeeded", "failed", "cancelled"))
    evals = _status_counts(db, EvalTask, pids,
                           ("pending", "running", "succeeded", "failed"))
    deps = _status_counts(db, Deployment, pids,
                          ("creating", "running", "stopped", "failed"))
    mtypes = dict(db.query(ModelAsset.type, func.count(ModelAsset.id))
                  .filter(ModelAsset.project_id.in_(pids)).group_by(ModelAsset.type).all())
    mtypes = {t: mtypes.get(t, 0) for t in ("base", "lora", "merged")}
    models_total = sum(mtypes.values())
    models_pending = db.query(ModelAsset).filter(
        ModelAsset.project_id.in_(pids), ModelAsset.status == "pending_download").count()

    # 数据量
    total_samples = (db.query(func.coalesce(func.sum(DatasetVersion.sample_count), 0))
                     .join(Dataset, DatasetVersion.dataset_id == Dataset.id)
                     .filter(Dataset.project_id.in_(pids)).scalar())
    dataset_bytes = (db.query(func.coalesce(func.sum(DatasetVersion.size_bytes), 0))
                     .join(Dataset, DatasetVersion.dataset_id == Dataset.id)
                     .filter(Dataset.project_id.in_(pids)).scalar())
    model_bytes = (db.query(func.coalesce(func.sum(ModelAsset.size_bytes), 0))
                   .filter(ModelAsset.project_id.in_(pids)).scalar())

    # 数据任务（解析/清洗/合成，全局）
    dt = dict(db.query(DataTask.status, func.count(DataTask.id))
              .group_by(DataTask.status).all())
    data_tasks = {"total": sum(dt.values()), "running": dt.get("pending", 0) + dt.get("running", 0),
                  "done": dt.get("done", 0), "failed": dt.get("failed", 0)}

    # 每卡最新指标（含 DCGM 扩展）
    gpus = []
    latest_ts = db.query(func.max(GpuMetric.ts)).scalar()
    if latest_ts:
        names = (node.meta or {}).get("names", []) if node else []
        for m in (db.query(GpuMetric).filter(GpuMetric.ts == latest_ts)
                  .order_by(GpuMetric.gpu_index).all()):
            e = m.extra or {}
            gpus.append({
                "index": m.gpu_index,
                "name": names[m.gpu_index] if m.gpu_index < len(names) else f"GPU {m.gpu_index}",
                "util": m.util, "mem_used": m.mem_used, "mem_total": m.mem_total,
                "power": m.power, "temp": m.temp,
                "sm_clock": e.get("sm_clock", 0), "mem_clock": e.get("mem_clock", 0),
                "mem_temp": e.get("mem_temp", 0),
                "health": e.get("health", 0), "xid": e.get("xid_error_count", 0),
                "remap": e.get("row_remap_pending", 0),
                "ecc_sbe": e.get("ecc_sbe_agg", 0), "ecc_dbe": e.get("ecc_dbe_agg", 0),
                "throttle": sum(1 for k in _THROTTLE_KEYS if e.get(k)),
            })

    # 最近评测 / 部署 / 操作日志
    recent_evals = []
    for ev in (db.query(EvalTask).filter(EvalTask.project_id.in_(pids))
               .order_by(EvalTask.id.desc()).limit(8).all()):
        sm = ev.summary or {}
        inner = sm.get("summary") if isinstance(sm.get("summary"), dict) else sm
        score = None
        for k in ("avg_score", "pass_rate", "judge_avg"):
            v = inner.get(k)
            if isinstance(v, (int, float)):
                score = round(float(v), 4)
                break
        recent_evals.append({
            "id": ev.id, "name": ev.name, "status": ev.status, "method": ev.method,
            "benchmark": ev.benchmark, "score": score,
            "created_at": str(ev.created_at),
            "finished_at": str(ev.finished_at) if ev.finished_at else "",
        })
    recent_depts = []
    for d, mname in (db.query(Deployment, ModelAsset.name)
                     .outerjoin(ModelAsset, Deployment.model_asset_id == ModelAsset.id)
                     .order_by(Deployment.id.desc()).limit(6).all()):
        recent_depts.append({
            "id": d.id, "name": d.name, "status": d.status, "engine": d.engine,
            "quant": d.quantization, "replicas": d.replicas, "model": mname or "",
            "endpoint": d.endpoint, "created_at": str(d.created_at),
        })
    recent_audit = [
        {"id": a.id, "username": a.username, "method": a.method, "path": a.path,
         "ip": a.ip, "status_code": a.status_code, "ts": a.ts}
        for a in db.query(AuditLog).order_by(AuditLog.id.desc()).limit(10).all()
    ]

    # 存储状态 + 磁盘占用
    storage = _storage_health()

    # 告警（前端按 code 做 i18n 文案）
    alerts = []
    if node and node.status == "offline":
        alerts.append({"level": "error", "code": "node_offline", "arg": node.name})
    if storage.get("enabled") and not storage.get("ok"):
        alerts.append({"level": "error", "code": "storage", "arg": storage.get("detail", "")})
    for g in gpus:
        if g["health"] >= 2:
            alerts.append({"level": "error", "code": "gpu_error", "arg": g["index"]})
        elif g["health"] == 1:
            alerts.append({"level": "warning", "code": "gpu_warn", "arg": g["index"]})
        if g["xid"] > 0:
            alerts.append({"level": "warning", "code": "gpu_xid", "arg": f'{g["index"]}:{int(g["xid"])}'})
        if g["remap"]:
            alerts.append({"level": "warning", "code": "gpu_remap", "arg": g["index"]})
    if jobs.get("failed"):
        alerts.append({"level": "warning", "code": "jobs_failed", "arg": jobs["failed"]})
    if models_pending:
        alerts.append({"level": "warning", "code": "models_pending", "arg": models_pending})

    return {
        "datasets": db.query(Dataset).filter(Dataset.project_id.in_(pids)).count(),
        "dataset_versions": db.query(DatasetVersion).join(Dataset).filter(Dataset.project_id.in_(pids)).count(),
        "total_samples": total_samples,
        "dataset_bytes": dataset_bytes,
        "model_bytes": model_bytes,
        "models": models_total,
        "models_by_type": mtypes,
        "models_pending": models_pending,
        "jobs_total": db.query(TrainingJob).filter(TrainingJob.project_id.in_(pids)).count(),
        "jobs_running": jobs["running"],
        "jobs_queued": jobs["queued"],
        "jobs_succeeded": jobs["succeeded"],
        "jobs_failed": jobs["failed"],
        "jobs_cancelled": jobs["cancelled"],
        "evals": sum(evals.values()),
        "evals_running": evals["pending"] + evals["running"],
        "evals_succeeded": evals["succeeded"],
        "evals_failed": evals["failed"],
        "deployments_running": deps["running"],
        "deployments_total": sum(deps.values()),
        "deployments_failed": deps["failed"],
        "data_tasks": data_tasks,
        "system": {
            "users": db.query(User).count(),
            "projects": db.query(Project).count(),
            "api_keys": db.query(ApiKey).filter(ApiKey.is_active.is_(True)).count(),
        },
        "gpu": {
            "node": node.name if node else "",
            "status": node.status if node else "unknown",
            "count": node.gpu_count if node else 0,
            "util": util, "mem_used": mem_used, "mem_total": mem_total,
        },
        "gpus": gpus,
        "recent_jobs": [
            {"id": j.id, "name": j.name, "stage": j.stage, "status": j.status,
             "created_at": str(j.created_at)}
            for j in (db.query(TrainingJob).filter(TrainingJob.project_id.in_(pids))
                      .order_by(TrainingJob.id.desc()).limit(8).all())
        ],
        "recent_evals": recent_evals,
        "recent_depts": recent_depts,
        "recent_audit": recent_audit,
        "alerts": alerts,
        # 对象存储状态（rustfs/S3；未配置时 ok=True 表示纯本地模式）
        "storage": storage,
    }


def _storage_health() -> dict:
    from ..services import storage
    try:
        h = storage.health()
    except Exception as e:  # 防御：健康检查本身不允许 500
        h = {"enabled": False, "endpoint": "", "bucket": "", "ok": False, "detail": str(e)[:200]}
    try:
        du = shutil.disk_usage(os.path.abspath(settings.data_dir))
        h["disk_used"] = du.used
        h["disk_total"] = du.total
        h["disk_free"] = du.free
    except Exception:
        pass
    return h

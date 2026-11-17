"""算力调度与运维：GPU 节点池、利用率监控、多任务排队与优先级、故障自愈视图

指标数据底座 = Prometheus（dcgm-exporter / node-exporter / 推理引擎 /metrics），
本模块只做查询与组装，不再读 GpuMetric/SystemMetric/InferenceMetric 旧表。
"""
import math
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..database import get_db
from ..security import get_current_user
from ..models import GpuNode, TrainingJob, Deployment
from .. import scheduler
from ..services import prom_service

router = APIRouter(prefix="/api/cluster", tags=["cluster"])


def _f(v, default=0.0):
    try:
        x = float(v)
    except (TypeError, ValueError):
        return default
    # Prometheus 可能返回 NaN（如空集上的 histogram_quantile）；非有限值无法 JSON 序列化
    # （starlette allow_nan=False → 500），按"无数据"处理
    return x if math.isfinite(x) else None


def _merged_series(exprs: list[tuple[str, str]], minutes: int, step: str = "60s") -> list[dict]:
    """多路 PromQL 区间查询 → 按 ts 合并的行 [{ts, <key>...}]（Prometheus 不可达时返回 []）"""
    per_ts: dict[int, dict] = {}
    for key, expr in exprs:
        try:
            results = prom_service.prom_range(expr, minutes=minutes, step=step)
        except Exception:
            continue
        for r in results:
            for ts, v in r.get("values", []):
                x = _f(v)
                if x is None:
                    continue  # NaN 数据点按缺失处理，不进序列
                t = int(float(ts))
                per_ts.setdefault(t, {"ts": t})[key] = x
    return [per_ts[t] for t in sorted(per_ts)]


def _gpu_series(exprs: list[tuple[str, str]], minutes: int) -> dict[str, list[dict]]:
    """按 gpu 标签分组的区间序列 {gpu_index: [{ts, <key>...}]}"""
    out: dict[int, dict[int, dict]] = {}
    for key, expr in exprs:
        try:
            results = prom_service.prom_range(expr, minutes=minutes, step="60s")
        except Exception:
            continue
        for r in results:
            g = r["metric"].get("gpu")
            if g is None:
                continue
            idx = int(g)
            for ts, v in r.get("values", []):
                x = _f(v)
                if x is None:
                    continue  # NaN 数据点按缺失处理，不进序列
                t = int(float(ts))
                out.setdefault(idx, {}).setdefault(t, {"ts": t})[key] = x
    return {str(i): [out[i][t] for t in sorted(out[i])] for i in sorted(out)}


@router.get("/nodes")
def nodes(db=Depends(get_db), user=Depends(get_current_user)):
    out = []
    for n in db.query(GpuNode).all():
        meta = n.meta or {}
        out.append({
            "id": n.id, "name": n.name, "gpu_count": n.gpu_count, "status": n.status,
            "last_heartbeat": str(n.last_heartbeat) if n.last_heartbeat else "",
            "meta": meta,
            "driver": meta.get("driver", ""),   # gpu-agent 采集（nvidia-smi）
            "cuda": meta.get("cuda", ""),
            "host": prom_service.host_snapshot(n.name),  # 宿主 CPU/内存/磁盘/网络
            "gpus": prom_service.gpu_snapshot(n),
            "gpu_in_use": scheduler._gpu_in_use(db),
        })
    return out


class NodeReq(BaseModel):
    name: str = ""
    gpu_count: int | None = None
    status: str = ""        # online/offline（空=不修改）
    meta: dict = {}         # 扩展信息：names / IB-RDMA 网络（ib_hca, net_ifname, gid_index...）


def _node_brief(n: GpuNode) -> dict:
    return {"id": n.id, "name": n.name, "gpu_count": n.gpu_count,
            "status": n.status, "meta": n.meta or {}}


@router.post("/nodes")
def register_node(req: NodeReq, db=Depends(get_db), user=Depends(get_current_user)):
    """注册计算节点（多机多卡集群纳管）。meta 可携带 IB/RDMA 网络信息。"""
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(400, "节点名不能为空")
    if db.query(GpuNode).filter_by(name=name).first():
        raise HTTPException(400, f"节点 {name} 已存在")
    n = GpuNode(
        name=name, gpu_count=max(0, req.gpu_count or 0),
        status=req.status if req.status in ("online", "offline") else "online",
        meta=req.meta or {},
    )
    db.add(n)
    db.commit()
    db.refresh(n)
    return _node_brief(n)


@router.put("/nodes/{nid}")
def update_node(nid: int, req: NodeReq, db=Depends(get_db), user=Depends(get_current_user)):
    n = db.get(GpuNode, nid)
    if not n:
        raise HTTPException(404, "节点不存在")
    if req.name and req.name.strip() != n.name:
        new_name = req.name.strip()
        if db.query(GpuNode).filter(GpuNode.name == new_name, GpuNode.id != nid).first():
            raise HTTPException(400, f"节点名 {new_name} 已被占用")
        n.name = new_name
    if req.gpu_count is not None:
        n.gpu_count = max(0, req.gpu_count)
    if req.status in ("online", "offline"):
        n.status = req.status
    if req.meta:
        n.meta = {**(n.meta or {}), **req.meta}
    db.commit()
    db.refresh(n)
    return _node_brief(n)


@router.delete("/nodes/{nid}")
def delete_node(nid: int, db=Depends(get_db), user=Depends(get_current_user)):
    n = db.get(GpuNode, nid)
    if not n:
        raise HTTPException(404, "节点不存在")
    if n.name == settings.node_name:
        raise HTTPException(400, "本节点不可删除")
    db.delete(n)
    db.commit()
    return {"ok": True}


@router.get("/nodes/{nid}/metrics")
def node_metrics(nid: int, minutes: int = 60, db=Depends(get_db), user=Depends(get_current_user)):
    """单节点 GPU 指标趋势（DCGM 区间查询；监控大屏主视图已改 Grafana 嵌入）"""
    n = db.get(GpuNode, nid)
    if not n:
        raise HTTPException(404, "节点不存在")
    series = _gpu_series([
        ("util", "DCGM_FI_DEV_GPU_UTIL"),
        ("temp", "DCGM_FI_DEV_GPU_TEMP"),
        ("power", "DCGM_FI_DEV_POWER_USAGE"),
        ("mem_used", "DCGM_FI_DEV_FB_USED"),
    ], minutes)
    return {"node": n.name, "series": series}


@router.get("/system")
def system_metrics(minutes: int = 60, db=Depends(get_db), user=Depends(get_current_user)):
    """宿主 Linux 系统指标（node-exporter）：最新快照 + CPU/内存/磁盘/负载趋势"""
    node = db.query(GpuNode).filter_by(name=settings.node_name).first()
    P = prom_service.PROMQL

    def inst(expr):
        try:
            r = prom_service.prom_instant(expr)
            return _f(r[0]["value"][1]) if r else None
        except Exception:
            return None

    cpu = inst(P["host_cpu"])
    mem = inst(P["host_mem"])
    disk = inst(P["host_disk"])
    latest = (None if cpu is None else {
        "ts": int(time.time()),
        "cpu_pct": cpu,
        "cpu_count": int(inst('count(count by (cpu)(node_cpu_seconds_total{mode="idle"}))') or 0),
        "mem_pct": mem if mem is not None else 0,
        "disk_pct": disk if disk is not None else 0,
        "load1": inst("node_load1") or 0,
        "load5": inst("node_load5") or 0,
        "load15": inst("node_load15") or 0,
        "uptime_s": inst("node_time_seconds - node_boot_time_seconds") or 0,
    })
    series = _merged_series([
        ("cpu", P["host_cpu"]),
        ("mem", P["host_mem"]),
        ("disk", P["host_disk"]),
        ("load1", "node_load1"),
    ], minutes)
    return {"node": node.name if node else "", "latest": latest, "series": series}


@router.get("/storage")
def storage_metrics(minutes: int = 60, db=Depends(get_db), user=Depends(get_current_user)):
    """宿主存储 IO（node-exporter）：读写吞吐 + IO 延时/利用率，最新快照 + 趋势。

    io_ms 空闲窗口为 NaN（0/0）——按"无数据"处理（None/不进序列），不会 500 也不误报告警。
    """
    node = db.query(GpuNode).filter_by(name=settings.node_name).first()
    P = prom_service.PROMQL

    def inst(expr):
        try:
            r = prom_service.prom_instant(expr)
            return _f(r[0]["value"][1]) if r else None
        except Exception:
            return None

    read = inst(P["host_disk_read"])
    write = inst(P["host_disk_write"])
    io_ms = inst(P["host_disk_io_ms"])
    io_util = inst(P["host_disk_util"])
    latest = (None if (read is None and write is None
                       and io_ms is None and io_util is None) else {
        "ts": int(time.time()),
        "read_bps": read, "write_bps": write,
        "io_ms": io_ms, "io_util_pct": io_util,
    })
    series = _merged_series([
        ("read_bps", P["host_disk_read"]),
        ("write_bps", P["host_disk_write"]),
        ("io_ms", P["host_disk_io_ms"]),
        ("io_util", P["host_disk_util"]),
    ], minutes)
    return {"node": node.name if node else "", "latest": latest, "series": series}


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
    """推理服务总览（vLLM/SGLang 指标按 docker_sd 的 deployment_id 标签过滤）"""
    deps = db.query(Deployment).filter(Deployment.status == "running").all()
    out = []
    for d in deps:
        sel = f'{{deployment_id="{d.id}"}}'
        kv_q = f"max(vllm:kv_cache_usage_perc{sel} or sglang:token_usage{sel}) * 100"
        run_q = f"max(vllm:num_requests_running{sel} or sglang:num_running_requests{sel})"
        wait_q = f"max(vllm:num_requests_waiting{sel} or sglang:num_queue_requests{sel})"
        lat_q = (f"histogram_quantile(0.95, sum by (le) "
                 f"(rate(vllm:e2e_request_latency_seconds_bucket{sel}[5m]))) * 1000")

        def inst(expr):
            try:
                r = prom_service.prom_instant(expr)
                return _f(r[0]["value"][1]) if r else None
            except Exception:
                return None

        kv = inst(kv_q)
        latest = None if kv is None else {
            "kv": kv, "running": int(inst(run_q) or 0), "waiting": int(inst(wait_q) or 0),
            "latency_ms": round(inst(lat_q) or 0, 1),
        }
        series = _merged_series([("kv", kv_q), ("running", run_q)], 60)
        out.append({"id": d.id, "name": d.name, "engine": d.engine,
                    "latest": latest, "series": series})
    return {"items": out}

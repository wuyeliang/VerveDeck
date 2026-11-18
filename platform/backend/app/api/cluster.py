"""算力调度与运维：GPU 节点池、利用率监控、多任务排队与优先级、故障自愈视图

指标数据底座 = Prometheus（dcgm-exporter / node-exporter / 推理引擎 /metrics），
本模块只做查询与组装，不再读 GpuMetric/SystemMetric/InferenceMetric 旧表。
"""
import secrets
import math
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..config import settings
from ..database import get_db
from ..security import get_current_user
from ..models import GpuNode, NodeToken, TrainingJob, Deployment
from .. import scheduler
from ..services import prom_service
from ..services import taskcontainer as tc

router = APIRouter(prefix="/api/cluster", tags=["cluster"])

# 节点注册令牌有效期（参照 GPUStack 注册令牌模式）：24h 内可完成"复制命令→目标机执行"
TOKEN_TTL_HOURS = 24


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
    local_rdma: dict | None = None
    for n in db.query(GpuNode).all():
        meta = n.meta or {}
        if n.name == settings.node_name:
            if local_rdma is None:
                try:
                    local_rdma = tc.rdma_info()
                except Exception:
                    local_rdma = {"present": False, "devices": []}
            rdma = local_rdma
        else:
            # 远端节点：gpu-agent 在宿主侧采集写 meta.ib_devices
            ib = [x.strip() for x in str(meta.get("ib_devices", "")).split(",") if x.strip()]
            devs = [d for d in ib if d.startswith("uverbs")]
            rdma = {"present": bool(devs), "devices": devs}
        out.append({
            "id": n.id, "name": n.name, "local": n.name == settings.node_name,
            "gpu_count": n.gpu_count, "status": n.status,
            "last_heartbeat": str(n.last_heartbeat) if n.last_heartbeat else "",
            "meta": meta,
            "driver": meta.get("driver", ""),   # gpu-agent 采集（nvidia-smi）
            "cuda": meta.get("cuda", ""),
            "rdma": rdma,                       # IB/RDMA 状态（present + uverbs 设备）
            "state_message": n.state_message or "",
            "maintenance": bool(n.maintenance),
            "maintenance_message": n.maintenance_message or "",
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
    token: str = ""         # 注册令牌（add-node.sh 携带；页面注册必须）
    maintenance: bool | None = None          # 维护模式开关（None=不修改）
    maintenance_message: str | None = None   # 维护原因


class TokenReq(BaseModel):
    node_name: str = ""
    gpu_count: int = 0     # 预期卡数（参考信息；实际以节点端采集为准）
    force: bool = False    # True=撤销该节点全部旧令牌并重新签发（页面「重新签发」；已注册节点也放行）


class HeartbeatReq(BaseModel):
    token: str = ""
    gpu_count: int | None = None   # 实时 nvidia-smi 卡数
    names: list[str] = []          # 每卡型号
    driver: str = ""
    cuda: str = ""
    ib_devices: str = ""           # 宿主 IB 设备（uverbs0,rdma_cm...）


def _node_brief(n: GpuNode) -> dict:
    return {"id": n.id, "name": n.name, "gpu_count": n.gpu_count,
            "status": n.status, "meta": n.meta or {},
            "state_message": n.state_message or "",
            "maintenance": bool(n.maintenance),
            "maintenance_message": n.maintenance_message or ""}


def _valid_token(db, token: str, node_name: str | None = None) -> NodeToken:
    """校验注册令牌：存在、未过期、（可选）绑定节点名一致；成功刷新 used_at 由调用方 commit"""
    tok = token.strip()
    if not tok:
        raise HTTPException(400, "缺少注册令牌")
    t = db.query(NodeToken).filter_by(token=tok).first()
    if not t:
        raise HTTPException(403, "注册令牌无效（可能已被撤销或重新签发）")
    if t.expires_at and t.expires_at < datetime.now():
        raise HTTPException(403, "注册令牌已过期，请在页面重新签发")
    if node_name and t.node_name != node_name:
        raise HTTPException(403, f"令牌绑定节点 {t.node_name}，与 {node_name} 不符")
    return t


@router.post("/nodes/token")
def issue_node_token(req: TokenReq, db=Depends(get_db), user=Depends(get_current_user)):
    """签发节点注册令牌（参照 GPUStack worker token）：绑定节点名、24h 有效、可重复使用。
    返回 token + 过期时间；接入命令由前端拼 window.location.origin 组装。
    已注册节点普通签发被拒（防令牌堆积）；force=True 时放行——撤销旧令牌发新令牌，
    供节点端重跑 add-node.sh 换令牌。"""
    name = (req.node_name or "").strip()
    if not name:
        raise HTTPException(400, "节点名不能为空")
    if name == settings.node_name:
        raise HTTPException(400, "本节点由 gpu-agent 自动纳管，无需签发令牌")
    if not req.force and db.query(GpuNode).filter_by(name=name).first():
        # 已注册节点：普通签发拒绝（防令牌堆积）；force=True 是"撤销并重签"，
        # 专门服务节点端重跑 add-node.sh 的场景，必须放行
        raise HTTPException(400, f"节点 {name} 已存在（如需重签请使用 force 重签）")
    now = datetime.now()
    if req.force:
        # 重新签发：撤销全部旧令牌（旧接入命令立即失效），发新令牌
        db.query(NodeToken).filter_by(node_name=name).delete(synchronize_session=False)
        db.flush()
    else:
        # 同节点名未过期的旧令牌直接复用（重复签发不堆积）
        live = db.query(NodeToken).filter_by(node_name=name).order_by(NodeToken.id.desc()).all()
        for t in live:
            if t.expires_at and t.expires_at > now:
                return {"token": t.token, "node_name": name,
                        "expires_at": str(t.expires_at), "reused": True}
    tok = NodeToken(token="vdk-" + secrets.token_hex(16), node_name=name,
                    expires_at=now + timedelta(hours=TOKEN_TTL_HOURS))
    db.add(tok)
    db.commit()
    db.refresh(tok)
    return {"token": tok.token, "node_name": name, "expires_at": str(tok.expires_at), "reused": False}


@router.get("/nodes/{nid}/tokens")
def list_node_tokens(nid: int, db=Depends(get_db), user=Depends(get_current_user)):
    """节点有效令牌（删除影响展示 + 重新查看接入命令用）"""
    n = db.get(GpuNode, nid)
    if not n:
        raise HTTPException(404, "节点不存在")
    now = datetime.now()
    rows = db.query(NodeToken).filter_by(node_name=n.name)\
        .filter(NodeToken.expires_at > now).order_by(NodeToken.id.desc()).all()
    return [{"id": t.id, "token": t.token, "expires_at": str(t.expires_at)} for t in rows]


@router.delete("/nodes/token/{tid}")
def revoke_node_token(tid: int, db=Depends(get_db), user=Depends(get_current_user)):
    """撤销令牌（重新签发用）；撤销后旧接入命令立即失效"""
    t = db.get(NodeToken, tid)
    if not t:
        raise HTTPException(404, "令牌不存在")
    db.delete(t)
    db.commit()
    return {"ok": True}


@router.post("/nodes")
def register_node(req: NodeReq, db=Depends(get_db), user=Depends(get_current_user)):
    """注册计算节点：凭签发令牌（add-node.sh 携带）。幂等——同令牌重复执行=刷新注册
    （机器重启/心跳进程丢失后重跑命令即恢复），meta 自动采集卡名/驱动/CUDA/IB。"""
    tok = _valid_token(db, req.token)
    name = tok.node_name  # 节点名以令牌绑定为准（防借令牌注册他人节点名）
    existed = db.query(GpuNode).filter_by(name=name).first()
    if existed:
        n = existed
    else:
        n = GpuNode(name=name, gpu_count=0)
        db.add(n)
        db.flush()
    meta = {**(n.meta or {})}
    if req.meta:
        meta.update({k: v for k, v in req.meta.items() if v not in ("", None, [])})
    n.meta = meta
    if req.gpu_count:
        n.gpu_count = max(0, req.gpu_count)
    n.status = "online"
    n.last_heartbeat = datetime.now()
    n.state_message = ""
    tok.used_at = datetime.now()
    db.commit()
    db.refresh(n)
    return _node_brief(n)


@router.post("/nodes/{nid}/heartbeat")
def node_heartbeat(nid: int, req: HeartbeatReq, db=Depends(get_db)):
    """节点端后台心跳（add-node.sh 守护循环每 10s 调）：凭令牌更新心跳时间 + 实时 GPU 快照。
    不经过用户登录态——节点守护进程只持有注册令牌。"""
    n = db.get(GpuNode, nid)
    if not n:
        raise HTTPException(404, "节点不存在")
    _valid_token(db, req.token, node_name=n.name)
    n.last_heartbeat = datetime.now()
    if req.gpu_count is not None:
        n.gpu_count = max(0, req.gpu_count)
    if n.status != "online" and not n.maintenance:
        n.status = "online"
        n.state_message = ""
    # 心跳携带的环境快照（瞬时读空不覆盖既有值，与 gpu_service 同策略）
    cur = n.meta or {}
    updates = {}
    if req.names:
        updates["names"] = req.names
    for k, v in (("driver", req.driver), ("cuda", req.cuda), ("ib_devices", req.ib_devices)):
        if v:
            updates[k] = v
    if updates:
        n.meta = {**cur, **updates}
    db.commit()
    return {"ok": True}


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
    # 维护模式（参照 GPUStack maintenance）：摘除服务——不判离线、不告警、调度不占容量
    if req.maintenance is not None:
        n.maintenance = bool(req.maintenance)
        n.maintenance_message = (req.maintenance_message or "").strip() if req.maintenance else ""
        if not n.maintenance:
            n.state_message = ""
    elif req.maintenance_message is not None and n.maintenance:
        n.maintenance_message = req.maintenance_message.strip()
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
    # 安全删除：撤销该节点全部注册令牌——否则节点端心跳进程仍在跑会立即"复活"重新注册
    revoked = db.query(NodeToken).filter_by(node_name=n.name).delete(synchronize_session=False)
    db.delete(n)
    db.commit()
    return {"ok": True, "revoked_tokens": revoked}


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


@router.get("/metrics/catalog")
def metrics_catalog_api(db=Depends(get_db), user=Depends(get_current_user)):
    """监控指标目录：全部实际采集的指标（名称/类别/来源/series 数）+ 汇总统计。
    「监控指标」页用（只读、登录可见；数据来自 Prometheus 实时查询，对齐行业采集面）。"""
    return prom_service.metrics_catalog()


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

"""GPU 节点采集：读取 gpu-agent 容器写出的 CSV（节点自愈：心跳超时判离线）"""
import csv
import io
import os
import time
from datetime import datetime

from ..config import settings
from ..database import SessionLocal
import logging

from ..models import GpuNode, GpuMetric, SystemMetric
from . import alert_service

log = logging.getLogger("gpu")

STALE_SECONDS = 60
_fail_count = 0
_last_ts = 0  # 已入库的最新采样时间戳：tick 周期(5s)与 agent 采集周期(5s)存在相位漂移，
# 同一份 CSV 可能被连续两次 tick 读到，ts 未变则跳过入库，避免同 ts 重复指标行
# （重复行会使 /api/cluster/nodes 的 ts==last_ts 查询返回 2 行，前端偶现 2 张 GPU）。
_last_host_ts = 0  # host.csv 同理

# DCGM 扩展字段（与 images/gpu-agent/collect.sh 的 HEADER 列一一对应）
EXTRA_KEYS = [
    "temp", "power", "power_limit", "enc_util", "dec_util", "gpu_util",
    "mem_util", "mem_copy_util", "mem_temp", "total_energy",
    "ecc_dbe_agg", "ecc_sbe_agg", "ecc_dbe_vol", "ecc_sbe_vol",
    "thermal_slowdown", "hw_slowdown", "hw_thermal_slowdown",
    "hw_power_brake_slowdown", "sw_thermal_slowdown",
    "fb_free", "fb_used", "fb_total",
    "sm_clock", "mem_clock", "video_clock",
    "health", "row_remap_pending", "xid_error_count",
]


def _f(v) -> float:
    """数值解析：N/A / 空 / 脏值一律按 0（DCGM 对不支持的字段输出 N/A）"""
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def read_agent() -> tuple[int, list[dict]]:
    """返回 (ts, gpus[{index,name,mem_used,mem_total,util,power,temp,extra{...DCGM 28 项}}])

    agent.csv 由 gpu-agent（DCGM 版）写出，29 列 = index + EXTRA_KEYS。
    """
    csv_path = os.path.join(settings.data_dir, "gpu", "agent.csv")
    ts_path = os.path.join(settings.data_dir, "gpu", "agent.ts")
    names_path = os.path.join(settings.data_dir, "gpu", "agent.names")
    names: dict[int, str] = {}
    try:
        with open(names_path) as f:
            for line in f:
                line = line.strip()
                if "," in line:
                    i, n = line.split(",", 1)
                    try:
                        names[int(float(i))] = n
                    except ValueError:
                        continue
    except (FileNotFoundError, OSError):
        pass
    try:
        with open(ts_path) as f:
            ts = int(f.read().strip() or time.time())
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            gpus = []
            for raw in reader:
                row = {k.strip(): (v.strip() if isinstance(v, str) else v)
                       for k, v in raw.items() if k is not None}
                try:
                    idx = int(float(row.get("index") or 0))
                    extra = {k: _f(row.get(k)) for k in EXTRA_KEYS}
                    gpus.append({
                        "index": idx,
                        "name": row.get("name") or names.get(idx, ""),
                        "mem_used": extra["fb_used"],
                        "mem_total": extra["fb_total"],
                        "util": extra["gpu_util"],
                        "power": extra["power"],
                        "temp": extra["temp"],
                        "extra": extra,
                    })
                except Exception:
                    continue
            return ts, gpus
    except FileNotFoundError:
        return 0, []
    except Exception:
        return 0, []


def read_host():
    """读取 host.csv（宿主 Linux 系统指标）。返回 (ts, dict) 或 None"""
    path = os.path.join(settings.data_dir, "gpu", "host.csv")
    try:
        with open(path) as f:
            row = next(csv.DictReader(f), None)
        if not row:
            return None
        ts = int(float(row.get("ts") or 0))
        if not ts:
            return None
        return ts, {
            "cpu_pct": _f(row.get("cpu_pct")),
            "cpu_count": int(_f(row.get("cpu_count"))),
            "load1": _f(row.get("load1")),
            "load5": _f(row.get("load5")),
            "load15": _f(row.get("load15")),
            "mem_total_mb": _f(row.get("mem_total_mb")),
            "mem_used_mb": _f(row.get("mem_used_mb")),
            "mem_pct": _f(row.get("mem_pct")),
            "disk_total_mb": _f(row.get("disk_total_mb")),
            "disk_used_mb": _f(row.get("disk_used_mb")),
            "disk_pct": _f(row.get("disk_pct")),
            "net_rx_mbps": _f(row.get("net_rx_mbps")),
            "net_tx_mbps": _f(row.get("net_tx_mbps")),
        }
    except (FileNotFoundError, OSError):
        return None
    except Exception:
        return None


def ensure_node(db) -> GpuNode:
    node = db.query(GpuNode).filter(GpuNode.name == settings.node_name).first()
    if not node:
        node = GpuNode(name=settings.node_name, gpu_count=0, status="online")
        db.add(node)
        db.commit()
        db.refresh(node)
    return node


def tick():
    """调度器每 tick 调用：更新节点心跳/状态/指标采样 + 宿主系统指标入库 + 离线/恢复事件"""
    global _last_ts, _fail_count, _last_host_ts
    db = SessionLocal()
    try:
        node = ensure_node(db)
        ts, gpus = read_agent()
        if gpus:
            _fail_count = 0
            node.gpu_count = len(gpus)
            node.last_heartbeat = datetime.fromtimestamp(ts)
            if node.status != "online":
                log.info("node %s back online", node.name)
                alert_service.trigger_event(db, "node_online", node.name,
                                            f"计算节点 {node.name} 恢复在线")
            node.status = "online"
            if not node.meta or node.meta.get("names") != [g["name"] for g in gpus]:
                node.meta = {"names": [g["name"] for g in gpus]}
            # 采样入库（ts 有变化才写，保留最近数据由清理任务控制）
            if ts > _last_ts:
                _last_ts = ts
                for g in gpus:
                    db.add(GpuMetric(node_id=node.id, ts=ts, gpu_index=g["index"],
                                     mem_used=g["mem_used"], mem_total=g["mem_total"],
                                     util=g["util"], power=g["power"], temp=g["temp"],
                                     extra=g.get("extra", {})))
        else:
            # 连续 2 次 tick 读不到心跳（~10s）且超过 STALE 才判离线，避免 CSV 写入瞬间误判
            _fail_count += 1
            stale = (not ts) or (time.time() - ts) > STALE_SECONDS
            if _fail_count >= 2 and stale:
                if node.status != "offline":
                    log.warning("node %s marked OFFLINE (heartbeat stale)", node.name)
                    alert_service.trigger_event(db, "node_offline", node.name,
                                                f"计算节点 {node.name} 离线",
                                                "gpu-agent 心跳超时（60s），节点 GPU 停止上报")
                node.status = "offline"
        # 宿主 Linux 系统指标（独立于 GPU 心跳：GPU 缺失时仍入库，系统监控可用）
        h = read_host()
        if h:
            hts, hv = h
            if hts > _last_host_ts:
                _last_host_ts = hts
                db.add(SystemMetric(node_id=node.id, ts=hts, **hv))
        db.commit()
    except Exception:
        db.rollback()
        log.exception("gpu_service.tick error")
    finally:
        db.close()

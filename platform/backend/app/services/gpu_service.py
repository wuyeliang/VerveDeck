"""GPU 节点采集：读取 gpu-agent 容器写出的 CSV（节点自愈：心跳超时判离线）"""
import csv
import io
import os
import time
from datetime import datetime

from ..config import settings
from ..database import SessionLocal
import logging

from ..models import GpuNode, GpuMetric

log = logging.getLogger("gpu")

STALE_SECONDS = 60
_fail_count = 0
_last_ts = 0  # 已入库的最新采样时间戳：tick 周期(5s)与 agent 采集周期(5s)存在相位漂移，
# 同一份 CSV 可能被连续两次 tick 读到，ts 未变则跳过入库，避免同 ts 重复指标行
# （重复行会使 /api/cluster/nodes 的 ts==last_ts 查询返回 2 行，前端偶现 2 张 GPU）。

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


def ensure_node(db) -> GpuNode:
    node = db.query(GpuNode).filter(GpuNode.name == settings.node_name).first()
    if not node:
        node = GpuNode(name=settings.node_name, gpu_count=0, status="online")
        db.add(node)
        db.commit()
        db.refresh(node)
    return node


def tick():
    """调度器每 tick 调用：更新节点心跳/状态/指标采样"""
    global _last_ts, _fail_count
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
                node.status = "offline"
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

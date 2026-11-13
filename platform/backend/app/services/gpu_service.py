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


def read_agent() -> tuple[int, list[dict]]:
    """返回 (ts, gpus[{index,name,mem_used,mem_total,util,power,temp}])"""
    csv_path = os.path.join(settings.data_dir, "gpu", "agent.csv")
    ts_path = os.path.join(settings.data_dir, "gpu", "agent.ts")
    try:
        with open(ts_path) as f:
            ts = int(f.read().strip() or time.time())
        with open(csv_path) as f:
            reader = csv.DictReader(f)
            gpus = []
            for raw in reader:
                # nvidia-smi 表头/值含前导空格（" name"、" 1"），统一 strip
                row = {}
                for k, v in raw.items():
                    if k is None:
                        continue
                    row[k.strip()] = v.strip() if isinstance(v, str) else v
                try:
                    gpus.append({
                        "index": int(float(row.get("index") or 0)),
                        "name": row.get("name") or "",
                        "mem_used": float(row.get("memory.used [MiB]") or 0),
                        "mem_total": float(row.get("memory.total [MiB]") or 0),
                        "util": float(row.get("utilization.gpu [%]") or 0),
                        "power": float(row.get("power.draw [W]") or 0),
                        "temp": float(row.get("temperature.gpu") or 0),
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
    db = SessionLocal()
    try:
        node = ensure_node(db)
        global _fail_count
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
            # 采样入库（每 tick 一次，保留最近数据由清理任务控制）
            for g in gpus:
                db.add(GpuMetric(node_id=node.id, ts=ts, gpu_index=g["index"],
                                 mem_used=g["mem_used"], mem_total=g["mem_total"],
                                 util=g["util"], power=g["power"], temp=g["temp"]))
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

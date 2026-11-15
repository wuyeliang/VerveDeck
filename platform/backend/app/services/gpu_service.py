"""GPU 节点心跳：读取 gpu-agent 写出的 agent.ts / agent.names（节点自愈：心跳超时判离线）

GPU 指标数据已由 dcgm-exporter 暴露给 Prometheus，本模块不再解析 CSV / 入库，
只负责节点在线状态与离线/恢复事件；实时指标查询见 prom_service.gpu_snapshot。
"""
import logging
import os
import time
from datetime import datetime

from ..config import settings
from ..database import SessionLocal

from ..models import GpuNode
from . import alert_service

log = logging.getLogger("gpu")

STALE_SECONDS = 60
_fail_count = 0

HEARTBEAT_FILE = os.path.join(settings.data_dir, "gpu", "agent.ts")
NAMES_FILE = os.path.join(settings.data_dir, "gpu", "agent.names")


def read_names() -> list[str]:
    """agent.names：每行 `index,name`（gpu-agent 启动时写一次）"""
    names: dict[int, str] = {}
    try:
        with open(NAMES_FILE) as f:
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
    if not names:
        return []
    return [names[i] for i in sorted(names)]


def read_heartbeat() -> int:
    """agent.ts：gpu-agent 每 5s 写一次的 epoch 秒；读不到返回 0"""
    try:
        with open(HEARTBEAT_FILE) as f:
            return int(f.read().strip() or 0)
    except (FileNotFoundError, OSError, ValueError):
        return 0


def ensure_node(db) -> GpuNode:
    node = db.query(GpuNode).filter(GpuNode.name == settings.node_name).first()
    if not node:
        node = GpuNode(name=settings.node_name, gpu_count=0, status="online")
        db.add(node)
        db.commit()
        db.refresh(node)
    return node


def tick():
    """调度器每 tick 调用：更新节点心跳/在线状态，触发离线/恢复事件（指标不再入库）"""
    global _fail_count
    db = SessionLocal()
    try:
        node = ensure_node(db)
        ts = read_heartbeat()
        if ts and time.time() - ts <= STALE_SECONDS:
            _fail_count = 0
            node.last_heartbeat = datetime.fromtimestamp(ts)
            names = read_names()
            if names:
                node.gpu_count = len(names)
                if not node.meta or node.meta.get("names") != names:
                    node.meta = {"names": names}
            if node.status != "online":
                log.info("node %s back online", node.name)
                alert_service.trigger_event(db, "node_online", node.name,
                                            f"计算节点 {node.name} 恢复在线")
            node.status = "online"
        else:
            # 连续 2 次 tick 读不到有效心跳（~10s）且超过 STALE 才判离线，避免写入瞬间误判
            _fail_count += 1
            stale = (not ts) or (time.time() - ts) > STALE_SECONDS
            if _fail_count >= 2 and stale:
                if node.status != "offline":
                    log.warning("node %s marked OFFLINE (heartbeat stale)", node.name)
                    alert_service.trigger_event(db, "node_offline", node.name,
                                                f"计算节点 {node.name} 离线",
                                                "gpu-agent 心跳超时（60s），节点停止上报")
                node.status = "offline"
        db.commit()
    except Exception:
        db.rollback()
        log.exception("gpu_service.tick error")
    finally:
        db.close()

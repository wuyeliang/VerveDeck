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

STALE_SECONDS = 60          # 本机 gpu-agent 心跳宽限（agent 每 5s 写一次）
REMOTE_GRACE_SECONDS = 90   # 远端节点心跳宽限（add-node.sh 守护每 10s 上报，~9 次丢失才判离线）
_fail_count = 0

HEARTBEAT_FILE = os.path.join(settings.data_dir, "gpu", "agent.ts")
NAMES_FILE = os.path.join(settings.data_dir, "gpu", "agent.names")
META_FILE = os.path.join(settings.data_dir, "gpu", "agent.meta")


def read_meta() -> dict:
    """agent.meta：`driver=` / `cuda=` / `ib_devices=` 键值行（gpu-agent 启动时写一次）；
    读不到按空值兜底。ib_devices：宿主 IB 设备名逗号分隔（uverbs0,uverbs1,rdma_cm），
    宿主无 RDMA 时为空（backend 据此回退探测并展示 RDMA 状态）"""
    out = {"driver": "", "cuda": "", "ib_devices": ""}
    try:
        with open(META_FILE) as f:
            for line in f:
                line = line.strip()
                if "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() in ("driver", "cuda", "ib_devices"):
                        out[k.strip()] = v.strip()
    except (FileNotFoundError, OSError):
        pass
    return out


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
    """调度器每 tick 调用：本机 + 远端节点心跳状态机（参照 GPUStack compute_state），
    触发离线/恢复事件；指标不再入库（Prometheus 采集）"""
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
            env = read_meta()
            # 组装完整 meta（瞬时读空时保留既有值，避免覆盖；变化才回写，减少 JSON 变更）
            cur = node.meta or {}
            new_meta = {
                "names": names if names else cur.get("names", []),
                "driver": env["driver"] or cur.get("driver", ""),
                "cuda": env["cuda"] or cur.get("cuda", ""),
                "ib_devices": env["ib_devices"] or cur.get("ib_devices", ""),
            }
            if new_meta != cur:
                node.meta = new_meta
            if node.status != "online":
                log.info("node %s back online", node.name)
                alert_service.trigger_event(db, "node_online", node.name,
                                            f"计算节点 {node.name} 恢复在线")
            node.status = "online"
            node.state_message = ""
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
                last = node.last_heartbeat.strftime("%Y-%m-%d %H:%M:%S") if node.last_heartbeat else "无"
                node.state_message = f"gpu-agent 心跳超时（{STALE_SECONDS}s），最后心跳 {last}；" \
                                     f"检查节点 {node.name} 上 gpu-agent 容器与平台栈"
        # 远端节点：心跳宽限状态机（维护模式豁免）
        _tick_remote_nodes(db)
        db.commit()
    except Exception:
        db.rollback()
        log.exception("gpu_service.tick error")
    finally:
        db.close()


def _tick_remote_nodes(db):
    """远端节点状态机（name != 本机）：last_heartbeat 超过 REMOTE_GRACE_SECONDS → offline + 事件 +
    state_message；心跳恢复 → online + 事件；maintenance 节点豁免（不判离线、不告警）。
    存量"纯登记"节点（从未有心跳）不强制判离线，仅挂提示引导补跑接入命令。"""
    now = datetime.now()
    for n in db.query(GpuNode).filter(GpuNode.name != settings.node_name).all():
        if n.maintenance:
            continue
        if n.last_heartbeat is None:
            # 存量登记节点：保持原状态，挂引导提示（避免批量误判离线）
            hint = "未上报心跳（存量登记节点）：在该机重跑 add-node.sh 接入命令即可启用实时状态"
            if n.state_message != hint:
                n.state_message = hint
            continue
        age = (now - n.last_heartbeat).total_seconds()
        if age > REMOTE_GRACE_SECONDS:
            if n.status != "offline":
                log.warning("node %s marked OFFLINE (remote heartbeat stale %ds)", n.name, age)
                alert_service.trigger_event(db, "node_offline", n.name,
                                            f"计算节点 {n.name} 离线",
                                            f"节点心跳超时（{REMOTE_GRACE_SECONDS}s）；"
                                            f"检查节点端心跳进程（ps -ef | grep vervedeck-node-agent；日志 /var/log/vervedeck-node-agent.log 或 ~/.vervedeck-node-agent.log），"
                                            f"或重跑接入命令恢复")
            n.status = "offline"
            last = n.last_heartbeat.strftime("%Y-%m-%d %H:%M:%S")
            n.state_message = (f"心跳超时（>{REMOTE_GRACE_SECONDS}s），最后心跳 {last}；"
                               f"节点端心跳进程可能已停止，重跑接入命令可恢复")
        else:
            if n.status != "online":
                log.info("node %s back online (remote heartbeat)", n.name)
                alert_service.trigger_event(db, "node_online", n.name,
                                            f"计算节点 {n.name} 恢复在线")
            n.status = "online"
            n.state_message = ""

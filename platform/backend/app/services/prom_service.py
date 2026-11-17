# -*- coding: utf-8 -*-
"""Prometheus / Alertmanager 集成层（监控告警数据底座）

职责：
- 查询：prom_instant / prom_range（Prometheus HTTP API）
- 渲染：「告警规则」UI（AlertRule.threshold 行）→ Prometheus 规则 YAML；
        「通知通道」配置（channel_config）→ Alertmanager YAML（邮件原生 + 企微走内部 webhook）
- 热加载：POST /-/reload（Prometheus / Alertmanager）
- 回调：Alertmanager → /api/internal/am-webhook（企微通知 + 告警事件同步；邮件由 AM 直接发）
- 快照：gpu_snapshot 从 DCGM_FI_DEV_* 即时查询组装 GPU 设备表数据

配置文件写入位置（compose 共享给对应容器，backend 启动/变更时渲染覆盖并热加载）：
  {DATA_DIR}/monitoring/rules/alerts.vergedeck.yml → prometheus rule_files
  {DATA_DIR}/monitoring/alertmanager.yml           → alertmanager --config.file
"""
import logging
import os
import re
import threading
import time

import requests
import yaml

from ..config import settings
from ..database import SessionLocal
from ..models import AlertRule, AlertEvent

log = logging.getLogger("prom")

# ---------- 指标 → PromQL（规则渲染与即时查询共用） ----------
# 值均为「百分比/秒/个」口径；op+threshold 由规则行提供，拼成 expr {op} {thr}
PROMQL = {
    "host_cpu": '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[2m])) * 100)',
    "host_mem": "(1 - (node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes)) * 100",
    "host_disk": ('100 * (1 - (node_filesystem_avail_bytes{mountpoint="/", fstype!~"tmpfs|overlay|squashfs"}'
                  ' / node_filesystem_size_bytes{mountpoint="/", fstype!~"tmpfs|overlay|squashfs"}))'),
    "host_net_rx": 'sum(rate(node_network_receive_bytes_total{device!="lo"}[2m]))',
    "host_net_tx": 'sum(rate(node_network_transmit_bytes_total{device!="lo"}[2m]))',
    # 存储 IO（磁盘读写吞吐 B/s；排除 loop/ram/sr 伪设备）。
    # 注意：io_ms 空闲时为 0/0=NaN——告警比较对 NaN 恒为假（不会误报），
    # API 侧 _f 把 NaN 转 None 按"无数据"处理；LVM（dm-*）双计对延时（比值）无影响，
    # 吞吐/利用率只会偏高，阈值语义仍成立
    "host_disk_read": 'sum(rate(node_disk_read_bytes_total{device!~"loop.*|ram.*|sr.*|fd.*"}[2m]))',
    "host_disk_write": 'sum(rate(node_disk_written_bytes_total{device!~"loop.*|ram.*|sr.*|fd.*"}[2m]))',
    "host_disk_io_ms": ('1000 * (sum(rate(node_disk_io_time_seconds_total[5m])) '
                        '/ (sum(rate(node_disk_reads_completed_total[5m])) '
                        '+ sum(rate(node_disk_writes_completed_total[5m]))))'),
    "host_disk_util": '100 * sum(rate(node_disk_io_time_seconds_total{device!~"loop.*|ram.*|sr.*|fd.*"}[5m]))',
    "gpu_util": "DCGM_FI_DEV_GPU_UTIL",
    "gpu_temp": "DCGM_FI_DEV_GPU_TEMP",
    "gpu_mem": "100 * DCGM_FI_DEV_FB_USED / (DCGM_FI_DEV_FB_USED + DCGM_FI_DEV_FB_FREE)",
    "gpu_mem_temp": "DCGM_FI_DEV_MEMORY_TEMP",
    "gpu_power": "DCGM_FI_DEV_POWER_USAGE",
    "gpu_xid": "increase(DCGM_FI_DEV_XID_ERRORS[5m])",
    "gpu_ecc_dbe": "increase(DCGM_FI_DEV_ECC_DBE_AGG_TOTAL[1h])",
    "kv_cache": "(max by (container) (vllm:kv_cache_usage_perc) * 100 or max by (container) (sglang:token_usage) * 100)",
    "infer_waiting": "max by (container) (vllm:num_requests_waiting or sglang:num_queue_requests)",
    "infer_latency_p95": "histogram_quantile(0.95, sum by (le, container) (rate(vllm:e2e_request_latency_seconds_bucket[5m])))",
    "infer_down": 'up{job="inference"}',
    "system_up": 'up{job=~"dcgm-exporter|node-exporter|backend"}',
}

METRIC_NAMES = {
    "host_cpu": "宿主 CPU",
    "host_mem": "宿主内存",
    "host_disk": "宿主磁盘",
    "host_disk_read": "宿主磁盘读吞吐(B/s)",
    "host_disk_write": "宿主磁盘写吞吐(B/s)",
    "host_disk_io_ms": "宿主磁盘 IO 延时(ms)",
    "host_disk_util": "宿主磁盘 IO 利用率(%)",
    "gpu_util": "GPU 利用率",
    "gpu_temp": "GPU 温度",
    "gpu_mem": "GPU 显存占用",
    "gpu_mem_temp": "GPU 显存温度",
    "gpu_power": "GPU 功耗",
    "gpu_xid": "GPU XID 错误",
    "gpu_ecc_dbe": "GPU ECC 双比特错误",
    "kv_cache": "推理 KV 缓存",
    "infer_waiting": "推理排队请求",
    "infer_latency_p95": "推理 P95 延迟(秒)",
    "infer_down": "推理服务在线",
    "system_up": "监控目标在线",
}

# 规则 → Prometheus alert 名（稳定 slug，阈值作后缀区分同指标多阈值规则）
_ALERT_SLUG = {
    "host_cpu": "HostCpuHigh", "host_mem": "HostMemHigh", "host_disk": "HostDiskHigh",
    "gpu_util": "GpuUtilHigh", "gpu_temp": "GpuTempHigh", "gpu_mem": "GpuMemHigh",
    "gpu_mem_temp": "GpuMemTempHigh", "gpu_power": "GpuPowerHigh",
    "gpu_xid": "GpuXidError", "gpu_ecc_dbe": "GpuEccDbe",
    "kv_cache": "KvCacheHigh", "infer_waiting": "InferQueueHigh",
    "infer_latency_p95": "InferLatencyP95High",
    "infer_down": "InferDown", "system_up": "MonitorTargetDown",
    "host_disk_read": "HostDiskReadHigh", "host_disk_write": "HostDiskWriteHigh",
    "host_disk_io_ms": "HostDiskIoMsHigh", "host_disk_util": "HostDiskUtilHigh",
}

RULES_DIR = os.path.join(settings.data_dir, "monitoring", "rules")
RULES_FILE = os.path.join(RULES_DIR, "alerts.vervedeck.yml")
AM_FILE = os.path.join(settings.data_dir, "monitoring", "alertmanager.yml")
# 推理容器 target 文件（file_sd）：每个部署一个文件 infer-<id>.yml，backend 在 start/stop 时写/删
INFER_SD_DIR = os.path.join(settings.data_dir, "monitoring", "sd")


# ---------- Prometheus 查询 ----------

def _prom(path: str, params: dict):
    r = requests.get(settings.prometheus_url + path, params=params, timeout=10)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise RuntimeError(f"prometheus {path}: {body.get('error', 'unknown error')}")
    return body["data"]["result"]


def prom_instant(promql: str):
    """即时查询，返回 [{metric:{...}, value:[ts, "val"]}]"""
    return _prom("/api/v1/query", {"query": promql})


def prom_range(promql: str, minutes: int = 60, step: str = "15s"):
    """区间查询，返回 [{metric:{...}, values:[[ts,"val"],...]}]"""
    end = int(time.time())
    return _prom("/api/v1/query_range",
                 {"query": promql, "start": end - minutes * 60, "end": end, "step": step})


def _f(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def gpu_snapshot(node=None):
    """各 GPU 最新快照（设备表用）：DCGM 即时查询组装，口径与旧 GpuMetric 行一致。

    卡片集以节点心跳登记的 GPU 列表（agent.names → node.meta.names，宿主 nvidia-smi
    实测）为准：每次渲染按当前列表 reconcile，列表外的陈旧序列（旧 exporter 容器代际
    残留、DCGM 重枚举的越界索引）一律不渲染成卡 —— GPU 设备页卡数恒等于宿主真实卡数。
    仅本节点（心跳采集对象）在心跳列表缺失时（agent 未就绪/尚未心跳）回退按 DCGM
    序列推导；其他节点（API 注册的远端节点、历史残留行）不渲染本集群 DCGM 数据，
    防止同一张物理卡以多个节点行重复出卡。

    返回 [{index, name, mem_used, mem_total, util, power, temp,
            sm_clock, mem_clock, mem_temp, xid, ecc_sbe, ecc_dbe, remap,
            health, throttle}]
    """
    names = (node.meta or {}).get("names", []) if node else []

    def _series(expr):
        """{gpu_index: value}；同索引多条序列同时可见（exporter 容器代际切换窗口）时
        取时间戳最新的样本，口径确定不随响应顺序漂移"""
        best: dict[int, tuple[float, float]] = {}
        try:
            for r in prom_instant(expr):
                g = r["metric"].get("gpu")
                if g is None:
                    continue
                ts = float(r["value"][0])
                cur = best.get(int(g))
                if cur is None or ts > cur[0]:
                    best[int(g)] = (ts, _f(r["value"][1]))
        except Exception:
            pass
        return {i: v for i, (_, v) in best.items()}

    util = _series("DCGM_FI_DEV_GPU_UTIL")
    fb_used = _series("DCGM_FI_DEV_FB_USED")
    fb_free = _series("DCGM_FI_DEV_FB_FREE")
    temp = _series("DCGM_FI_DEV_GPU_TEMP")
    mem_temp = _series("DCGM_FI_DEV_MEMORY_TEMP")
    power = _series("DCGM_FI_DEV_POWER_USAGE")
    power_limit = _series("DCGM_FI_DEV_POWER_MGMT_LIMIT")  # 功耗限额（W），GeForce 卡亦导出
    sm_clock = _series("DCGM_FI_DEV_SM_CLOCK")
    mem_clock = _series("DCGM_FI_DEV_MEM_CLOCK")
    ecc_sbe = _series("DCGM_FI_DEV_ECC_SBE_AGG_TOTAL")
    ecc_dbe = _series("DCGM_FI_DEV_ECC_DBE_AGG_TOTAL")
    # 实际导出字段名为复数 XID_ERRORS（实测 3080 无此字段 → 0 兜底；数据中心卡有值）
    xid = _series("DCGM_FI_DEV_XID_ERRORS")
    # 降频判定用 10 分钟窗口内的真实降频增量（功耗/过热/主板限电）；
    # LOW_UTIL/同步降频属于空闲常态，不算故障信号。SLOWDOWN_DURATION/THROTTLE_REASONS
    # 为数据中心字段，3080 不导出。GeForce 卡 POWER_VIOLATION 是垃圾计数器（increase 可达 1e10，
    # 实测 3080 空闲仍增长），故加上限 900s（10 分钟窗口真实降频 ≤600s）过滤，数据中心卡不受影响
    slow_pwr = _series("increase(DCGM_FI_DEV_POWER_VIOLATION[10m])")
    slow_thm = _series("increase(DCGM_FI_DEV_THERMAL_VIOLATION[10m])")
    slow_brd = _series("increase(DCGM_FI_DEV_BOARD_LIMIT_VIOLATION[10m])")
    remap = _series("DCGM_FI_DEV_ROW_REMAP_PENDING")
    if names:
        # 心跳 reconcile：卡片集 = 心跳实测的当前 GPU 列表（幂等去重，每次渲染执行）；
        # DCGM 序列里越界的陈旧索引被清理，列表内暂无指标的卡给空行（exporter 未就绪）
        indexes = list(range(len(names)))
    elif node is None or node.name == settings.node_name:
        # 仅本节点允许回退 DCGM 序列（心跳前的短暂窗口）；远端/残留节点不渲染本集群数据
        indexes = sorted(set(util) | set(fb_used) | set(fb_free) | set(temp) | set(power))
    else:
        indexes = []
    out = []
    for i in indexes:
        total = fb_used.get(i, 0) + fb_free.get(i, 0)
        out.append({
            "index": i,
            "name": names[i] if i < len(names) else f"GPU {i}",
            "util": util.get(i, 0), "mem_used": fb_used.get(i, 0), "mem_total": total,
            "power": power.get(i, 0), "power_limit": power_limit.get(i, 0), "temp": temp.get(i, 0),
            "sm_clock": sm_clock.get(i, 0), "mem_clock": mem_clock.get(i, 0),
            "mem_temp": mem_temp.get(i, 0),
            "xid": xid.get(i, 0), "ecc_sbe": ecc_sbe.get(i, 0), "ecc_dbe": ecc_dbe.get(i, 0),
            "remap": remap.get(i, 0),
            # DCGM 健康位未随 dcgm-exporter 暴露，保持 0（与旧采集口径一致）
            "health": 0,
            # 上限 900s 过滤 GeForce 垃圾计数器（真实 10 分钟窗口降频 ≤600s）
            "throttle": 1 if 0 < (slow_pwr.get(i, 0) + slow_thm.get(i, 0) + slow_brd.get(i, 0)) <= 900 else 0,
        })
    return out


def host_snapshot(instance: str | None = None) -> dict:
    """宿主 CPU/内存/磁盘/网络即时快照（node-exporter），供计算节点卡展示四指标。

    Prometheus 不可达时各字段为 None（前端显示 -）。instance 用于多节点时按主机名筛选
    （node-exporter 的 instance 标签形如 `host:9100`）；单节点/未命中时回退首条结果。
    """
    P = PROMQL

    def pick(expr: str):
        try:
            rows = prom_instant(expr)
        except Exception:
            return None
        if not rows:
            return None
        if instance:
            for r in rows:
                inst = str(r.get("metric", {}).get("instance", ""))
                if inst.split(":")[0] == instance or instance in inst:
                    return _f(r["value"][1])
        return _f(rows[0]["value"][1])

    disk_total = None
    try:
        rows = prom_instant(
            'sum(node_filesystem_size_bytes{mountpoint="/", fstype!~"tmpfs|overlay|squashfs"})')
        disk_total = _f(rows[0]["value"][1]) / (1024 ** 3) if rows else None
    except Exception:
        pass

    return {
        "cpu_pct": pick(P["host_cpu"]),
        "mem_pct": pick(P["host_mem"]),
        "disk_pct": pick(P["host_disk"]),
        "net_rx_bps": pick(P["host_net_rx"]),
        "net_tx_bps": pick(P["host_net_tx"]),
        "disk_total_gb": disk_total,
    }


# ---------- 规则渲染（UI 阈值规则 → Prometheus 规则文件） ----------

def render_rules_yaml(rules) -> str:
    items = []
    used = set()
    for r in rules:
        expr = PROMQL.get(r.metric or "")
        if not expr:
            continue
        op = r.op or ">"
        thr = r.threshold if r.threshold is not None else 0
        base = _ALERT_SLUG.get(r.metric, (r.metric or "Metric").replace("_", "").capitalize() + "Alert")
        name = f"{base}{int(thr) if float(thr) == int(thr) else thr}"
        if name in used:  # 同指标同阈值多条规则时追加 ID 保证唯一
            name = f"{name}R{r.id}"
        used.add(name)
        sev = r.severity if r.severity in ("warning", "critical") else "warning"
        mname = METRIC_NAMES.get(r.metric, r.metric)
        items.append({
            "alert": name,
            "expr": f"{expr} {op} {thr:g}",
            "for": "60s",
            "labels": {"severity": sev, "source": "vervedeck", "metric": r.metric},
            "annotations": {
                "summary": f"{r.name}：{mname} {op} {thr:g}",
                "description": f"{r.name}：{mname} 当前值 {{{{ $value }}}}, 条件 {op} {thr:g}",
            },
        })
    return yaml.safe_dump({"groups": [{"name": "vervedeck-alerts", "rules": items}]},
                          allow_unicode=True, sort_keys=False)


# ---------- Alertmanager 渲染（通知通道 DB 配置 → AM YAML） ----------

def render_am_yaml(cfg: dict) -> str:
    has_email = bool(cfg.get("smtp_enabled") and cfg.get("smtp_host")
                     and cfg.get("smtp_user") and (cfg.get("smtp_to") or cfg.get("smtp_from")))
    has_wecom = bool(cfg.get("wecom_enabled") and cfg.get("wecom_webhook"))
    g: dict = {"resolve_timeout": "5m"}
    if has_email:
        g["smtp_host"] = cfg["smtp_host"]
        g["smtp_port"] = int(cfg.get("smtp_port") or 465)
        g["smtp_from"] = cfg.get("smtp_from") or cfg["smtp_user"]
        g["smtp_auth_username"] = cfg["smtp_user"]
        g["smtp_auth_password"] = cfg.get("smtp_password") or ""
        g["smtp_require_tls"] = False
        g["smtp_hello"] = "vervedeck"
    receivers = []
    if has_email or has_wecom:
        rcv: dict = {"name": "default"}
        if has_email:
            rcv["email_configs"] = [{
                "to": cfg.get("smtp_to") or cfg.get("alert_email_to") or cfg.get("smtp_from"),
                "send_resolved": True,
                "headers": {"Subject": "[VerveDeck] {{ .Status | toUpper }} {{ .CommonLabels.alertname }}"},
            }]
        if has_wecom:
            rcv["webhook_configs"] = [{
                "url": settings.backend_internal_url + "/api/internal/am-webhook",
                "send_resolved": True,
                "max_alerts": 50,
                "http_headers": {"X-Vervedeck-Token": settings.am_webhook_token},
            }]
        receivers.append(rcv)
    if not receivers:
        # 未配置任何通道：AM 不允许 receiver: null，用名为 "null" 的空 receiver 兜底（不产生通知）
        receivers = [{"name": "null"}]
    receiver = receivers[0]["name"]
    am = {
        "global": g,
        "route": {
            "receiver": receiver,
            "group_by": ["alertname", "instance"],
            "group_wait": "10s",
            "group_interval": "5m",
            "repeat_interval": "4h",
            "routes": [
                {"matchers": ['severity="critical"'], "receiver": receiver, "repeat_interval": "1h"},
            ],
        },
        "receivers": receivers,
    }
    return yaml.safe_dump(am, allow_unicode=True, sort_keys=False)


# ---------- 写文件 + 热加载 ----------

def _atomic_write(path: str, content: str):
    # 注意：必须原地写（保留 inode）。alertmanager.yml 等文件被容器 bind mount，
    # os.replace 换新 inode 后容器内仍见旧内容（docker bind mount 绑定 inode 而非路径）。
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())


def _reload(url: str, what: str, tries: int = 8, delay: float = 3.0) -> bool:
    for _ in range(tries):
        try:
            r = requests.post(url + "/-/reload", timeout=5)
            if r.status_code in (200, 204):
                return True
        except Exception:
            pass
        time.sleep(delay)
    log.warning("reload %s 失败（组件可能尚未启动，下次变更重试）", what)
    return False


def sync_rules(db=None) -> bool:
    """把启用中的阈值规则渲染为 Prometheus 规则文件并热加载"""
    own = db is None
    db = db or SessionLocal()
    try:
        rules = (db.query(AlertRule)
                 .filter(AlertRule.enabled == True, AlertRule.kind == "threshold").all())
    finally:
        if own:
            db.close()
    try:
        _atomic_write(RULES_FILE, render_rules_yaml(rules))
    except Exception:
        log.exception("渲染告警规则文件失败")
        return False
    return _reload(settings.prometheus_url, "prometheus")


def sync_channels() -> bool:
    """把通知通道配置渲染为 Alertmanager YAML 并热加载"""
    from . import alert_service
    try:
        _atomic_write(AM_FILE, render_am_yaml(alert_service.channel_config()))
    except Exception:
        log.exception("渲染 Alertmanager 配置失败")
        return False
    return _reload(settings.alertmanager_url, "alertmanager")


# ---------- 推理 target 文件（file_sd：backend 自持，替代 docker_sd） ----------

def infer_sd_file(dep_id: int) -> str:
    return os.path.join(INFER_SD_DIR, f"infer-{dep_id}.yml")


def write_infer_sd(dep_id: int, engine: str, container: str, targets: list[str]):
    """登记推理部署的 Prometheus target（每个副本 ip:8000 一条）"""
    if not targets:
        return
    doc = [{"targets": list(targets),
            "labels": {"deployment_id": str(dep_id), "engine": engine, "container": container}}]
    _atomic_write(infer_sd_file(dep_id), yaml.safe_dump(doc, sort_keys=False))


def drop_infer_sd(dep_id: int):
    try:
        os.remove(infer_sd_file(dep_id))
    except FileNotFoundError:
        pass


def resync_infer_sd(db=None):
    """全量幂等同步：按 DB 中活跃部署重建 target 文件（backend 重启/容器 IP 变化后修复用）"""
    from ..models import Deployment
    own = db is None
    db = db or SessionLocal()
    try:
        deps = (db.query(Deployment)
                .filter(Deployment.status.in_(["running", "creating"])).all())
        for d in deps:
            tgs = []
            if d.endpoint:
                tgs.append(d.endpoint)
            if d.container_ids:
                from . import taskcontainer as tc
                for i, cid in enumerate(d.container_ids):
                    ip = tc.container_ip(cid)
                    if ip and f"{ip}:8000" not in tgs:
                        tgs.append(f"{ip}:8000")
            if tgs:
                write_infer_sd(d.id, d.engine, f"vervedeck-infer-{d.id}-0", tgs)
            else:
                drop_infer_sd(d.id)  # 状态活跃但无可用端点（容器未起好），清掉旧 target
    finally:
        if own:
            db.close()


# ---------- Alertmanager 回调（企微通知 + 事件同步；邮件由 AM 原生发出） ----------

def am_webhook(db, payload: dict) -> dict:
    from . import alert_service
    status = str(payload.get("status") or "firing")
    alerts = payload.get("alerts") or []
    cfg = alert_service.channel_config()
    send_wecom = bool(cfg.get("wecom_enabled") and cfg.get("wecom_webhook"))
    fired = 0
    now = int(time.time())
    for a in alerts:
        labels = a.get("labels") or {}
        ann = a.get("annotations") or {}
        alertname = labels.get("alertname") or "unknown"
        sev = labels.get("severity") or "warning"
        target = labels.get("instance") or labels.get("container") or labels.get("job") or "node"
        summary = ann.get("summary") or alertname
        desc = str(ann.get("description") or "")[:1500]
        if status == "resolved":
            title = f"{alertname}：已恢复 {summary}"
            level = "info"
        else:
            title = f"{alertname}：{summary}"
            level = sev if sev in ("warning", "critical") else "warning"
            # 去重：同 alertname+目标 300s 内不重复记录/通知（AM 自身有 group/repeat 节奏）
            dup = (db.query(AlertEvent.id)
                   .filter(AlertEvent.target == target[:128],
                           AlertEvent.ts >= now - 300,
                           AlertEvent.title.like(f"{alertname}%")).count() > 0)
            if dup:
                continue
        ev = AlertEvent(ts=now, rule_id=None, level=level, target=target[:128],
                        title=title[:255], detail=desc)
        db.add(ev)
        db.commit()
        if status == "firing":
            fired += 1
        if send_wecom:
            body = f"**{'[已恢复] ' if status == 'resolved' else ''}{title}**\n{desc or summary}"
            threading.Thread(target=alert_service._send_wecom,
                             args=(title, body, cfg), daemon=True).start()
    return {"ok": True, "status": status, "fired": fired, "total": len(alerts)}

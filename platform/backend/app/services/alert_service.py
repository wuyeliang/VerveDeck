"""告警服务：阈值规则（系统/GPU/推理指标）+ 平台事件（节点离线、任务失败、推理不可用）

- evaluate(db)：调度器每 tick 调用，检查启用的阈值规则
- trigger_event(db, code, target, title, detail)：事件型触发（调度器/采集层在故障收敛点调用）
- 去重：同规则同目标在 cooldown 秒内不重复触发
- 通知：企业微信群机器人 webhook + SMTP 邮件，后台线程发送（不阻塞调度器 tick）
"""
import logging
import smtplib
import threading
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests
from sqlalchemy import func

from ..config import settings
from ..database import SessionLocal
from ..models import (AlertRule, AlertEvent, SystemMetric, GpuMetric, GpuNode,
                      InferenceMetric, Deployment)

log = logging.getLogger("alert")

OPS = {
    ">": lambda v, t: v > t,
    ">=": lambda v, t: v >= t,
    "<": lambda v, t: v < t,
    "<=": lambda v, t: v <= t,
}

METRIC_NAMES = {
    "host_cpu": "宿主 CPU",
    "host_mem": "宿主内存",
    "host_disk": "宿主磁盘",
    "gpu_util": "GPU 利用率",
    "gpu_temp": "GPU 温度",
    "gpu_mem": "GPU 显存占用",
    "kv_cache": "推理 KV 缓存占用",
}

EVENT_NAMES = {
    "node_offline": "计算节点离线",
    "node_online": "计算节点恢复在线",
    "train_failed": "训练任务失败",
    "eval_failed": "评测任务失败",
    "quant_failed": "量化任务失败",
    "infer_down": "推理服务不可用",
}


# ---------- 指标取值（取最新采样行） ----------

def _host_row(db):
    node = db.query(GpuNode).filter_by(name=settings.node_name).first()
    if not node:
        return None
    return (db.query(SystemMetric)
            .filter(SystemMetric.node_id == node.id)
            .order_by(SystemMetric.ts.desc()).first())


def _gpu_rows(db):
    node = db.query(GpuNode).filter_by(name=settings.node_name).first()
    if not node:
        return []
    last_ts = db.query(func.max(GpuMetric.ts)).filter(GpuMetric.node_id == node.id).scalar()
    if not last_ts:
        return []
    rows = (db.query(GpuMetric).filter(GpuMetric.node_id == node.id,
                                       GpuMetric.ts == last_ts).all())
    out = []
    seen = set()
    for r in rows:
        if r.gpu_index in seen:
            continue
        seen.add(r.gpu_index)
        out.append({
            "gpu": r.gpu_index,
            "util": r.util or 0,
            "temp": r.temp or 0,
            "mem_pct": (r.mem_used / r.mem_total * 100) if r.mem_total else 0,
        })
    return out


def _kv_rows(db):
    deps = db.query(Deployment).filter(Deployment.status == "running").all()
    out = []
    for d in deps:
        row = (db.query(InferenceMetric).filter(InferenceMetric.deployment_id == d.id)
               .order_by(InferenceMetric.ts.desc()).first())
        if row:
            out.append({"dep": d, "kv": row.kv_cache_pct or 0})
    return out


# ---------- 通知通道 ----------

def _channels(rule) -> list[str]:
    """规则指定通道优先；未指定 = 全部已配置通道"""
    def _ok(c):
        return (c == "wecom" and settings.wecom_webhook) or \
               (c == "email" and settings.smtp_host and settings.smtp_user)
    if rule.channels:
        return [c for c in rule.channels if _ok(c)]
    out = []
    if settings.wecom_webhook:
        out.append("wecom")
    if settings.smtp_host and settings.smtp_user:
        out.append("email")
    return out


def _send_wecom(title: str, body: str):
    r = requests.post(settings.wecom_webhook, json={
        "msgtype": "markdown",
        "markdown": {"content": f"**{title}**\n{body[:4000]}"},
    }, timeout=5)
    data = {}
    try:
        data = r.json()
    except ValueError:
        pass
    if r.status_code != 200 or data.get("errcode", 0) != 0:
        raise RuntimeError(f"webhook resp: {r.text[:200]}")


def _send_email(title: str, body: str):
    to = [x.strip() for x in settings.alert_email_to.split(",") if x.strip()]
    if not to:
        raise RuntimeError("ALERT_EMAIL_TO 未配置收件人")
    frm = settings.smtp_from or settings.smtp_user
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr((str(Header("VerveDeck", "utf-8")), frm))
    msg["To"] = ", ".join(to)
    if settings.smtp_port == 465:
        s = smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port, timeout=10)
    else:
        s = smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10)
        s.starttls()
    try:
        s.login(settings.smtp_user, settings.smtp_password)
        s.sendmail(frm, to, msg.as_string())
    finally:
        s.quit()


def _do_notify(rule, ev) -> bool:
    """同步发送（测试接口用）；生产走 _notify 线程包装。返回是否任一通道成功"""
    title = f"VerveDeck 告警 [{ev.level}] {ev.title}"
    body = (f"目标: {ev.target}\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            + (ev.detail or ""))
    sent = False
    for ch in _channels(rule):
        try:
            if ch == "wecom":
                _send_wecom(title, body)
            elif ch == "email":
                _send_email(title, body)
            sent = True
        except Exception as e:
            log.warning("alert notify %s failed: %s", ch, e)
    db = SessionLocal()
    try:
        e2 = db.get(AlertEvent, ev.id)
        if e2:
            e2.notified = sent
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
    return sent


def _notify(rule, ev):
    threading.Thread(target=_do_notify, args=(rule, ev), daemon=True).start()


# ---------- 触发 ----------

def _cooldown_hit(db, rule, target) -> bool:
    if not rule.cooldown:
        return False
    since = int(time.time()) - int(rule.cooldown)
    return (db.query(AlertEvent.id)
            .filter(AlertEvent.rule_id == rule.id, AlertEvent.target == target,
                    AlertEvent.ts >= since).count() > 0)


def fire(db, rule, target: str, title: str, detail: str = ""):
    """记录告警事件（cooldown 去重）+ 异步通知；被去重时返回 None"""
    if _cooldown_hit(db, rule, target):
        return None
    ev = AlertEvent(ts=int(time.time()), rule_id=rule.id, level=rule.severity,
                    target=target, title=title[:255], detail=(detail or "")[:2000])
    db.add(ev)
    db.commit()
    db.refresh(ev)
    log.warning("ALERT %s [%s] %s: %s", target, ev.level, ev.title, (detail or "")[:200])
    _notify(rule, ev)
    return ev


def _check(db, rule, target: str, value: float, desc: str):
    fn = OPS.get(rule.op or ">")
    if fn and fn(value, rule.threshold or 0):
        fire(db, rule, target,
             f"{target} {METRIC_NAMES.get(rule.metric, rule.metric)} {rule.op} {rule.threshold}",
             desc)


def evaluate(db):
    """阈值规则检查（调度器每 tick 调用；无启用规则时零开销）"""
    rules = (db.query(AlertRule)
             .filter(AlertRule.enabled == True, AlertRule.kind == "threshold").all())
    if not rules:
        return
    host = None
    gpus = None
    kvs = None
    for rule in rules:
        try:
            if rule.metric in ("host_cpu", "host_mem", "host_disk"):
                host = host or _host_row(db)
                if not host:
                    continue
                if rule.metric == "host_cpu":
                    _check(db, rule, "node", host.cpu_pct,
                           f"CPU 使用率 {host.cpu_pct:.0f}%（{host.cpu_count} 核）")
                elif rule.metric == "host_mem":
                    _check(db, rule, "node", host.mem_pct,
                           f"内存 {host.mem_pct:.0f}%（{host.mem_used_mb / 1024:.0f}/{host.mem_total_mb / 1024:.0f} GB）")
                else:
                    _check(db, rule, "node", host.disk_pct,
                           f"磁盘 {host.disk_pct:.0f}%（{host.disk_used_mb / 1024 / 1024:.1f}/{host.disk_total_mb / 1024 / 1024:.1f} GB）")
            elif rule.metric in ("gpu_util", "gpu_temp", "gpu_mem"):
                gpus = gpus or _gpu_rows(db)
                for g in gpus:
                    v = g[rule.metric]
                    unit = {"gpu_util": f"利用率 {v:.0f}%",
                            "gpu_temp": f"温度 {v:.0f}°C",
                            "gpu_mem": f"显存 {v:.0f}%"}[rule.metric]
                    _check(db, rule, f"gpu:{g['gpu']}", v, unit)
            elif rule.metric == "kv_cache":
                kvs = kvs or _kv_rows(db)
                for k in kvs:
                    _check(db, rule, f"deploy:{k['dep'].id}", k["kv"],
                           f"{k['dep'].name} KV 缓存占用 {k['kv']:.0f}%")
        except Exception:
            log.exception("alert rule %s evaluate error", rule.name)


def trigger_event(db, code: str, target: str, title: str, detail: str = ""):
    """事件型告警：调度器在故障收敛点调用（node_offline/train_failed/...）"""
    if code not in EVENT_NAMES:
        return
    rules = (db.query(AlertRule)
             .filter(AlertRule.enabled == True, AlertRule.kind == "event",
                     AlertRule.event == code).all())
    for rule in rules:
        try:
            fire(db, rule, target, title, detail)
        except Exception:
            log.exception("trigger_event %s error", code)


def test_notify(db, rule_id: int) -> dict:
    """测试接口：同步发送一条测试通知，返回结果"""
    rule = db.get(AlertRule, rule_id)
    if not rule:
        return {"ok": False, "error": "规则不存在"}
    chs = _channels(rule)
    if not chs:
        return {"ok": False, "error": "无可用通知通道（未配置 WECOM_WEBHOOK / SMTP_*）"}
    ev = AlertEvent(ts=int(time.time()), rule_id=rule.id, level="info",
                    target="test", title="[测试告警] 通知通道测试",
                    detail=f"规则: {rule.name}\n通道: {', '.join(chs)}")
    db.add(ev)
    db.commit()
    db.refresh(ev)
    ok = _do_notify(rule, ev)
    return {"ok": ok, "channels": chs, "notified": ok}

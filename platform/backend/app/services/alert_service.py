"""告警服务：事件型告警（节点离线、任务失败等 PromQL 无法表达的故障收敛点）+ 通知通道配置

阈值类监控告警已整体迁到 Prometheus/Alertmanager（见 prom_service）：
- 「告警规则」UI 的阈值行 → 渲染为 Prometheus 规则 YAML（/data/monitoring/rules）
- 「通知通道」配置 → 渲染为 Alertmanager YAML（/data/monitoring/alertmanager.yml）
- 邮件由 Alertmanager 原生发出；企微经 backend 内部 webhook（/api/internal/am-webhook）

本模块保留：
- trigger_event(db, code, target, title, detail)：事件型触发（调度器/采集层在故障收敛点调用）
- 通知通道 DB 配置（channel_config/get_config/save_config，DB 优先 env 兜底）
- _send_wecom / _send_email / test_channel / test_notify（通道测试与手动通知）
"""
import logging
import smtplib
import threading
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formataddr

import requests

from ..config import settings
from ..database import SessionLocal
from ..models import AlertRule, AlertEvent, AlertChannelConfig

log = logging.getLogger("alert")

EVENT_NAMES = {
    "node_offline": "计算节点离线",
    "node_online": "计算节点恢复在线",
    "train_failed": "训练任务失败",
    "eval_failed": "评测任务失败",
    "quant_failed": "量化任务失败",
    "infer_down": "推理服务不可用",
}


# ---------- 通知通道（DB 配置优先，环境变量兜底） ----------

PW_MASK = "******"


def _cfg_row():
    db = SessionLocal()
    try:
        return db.get(AlertChannelConfig, 1)
    except Exception:
        return None
    finally:
        db.close()


def channel_config() -> dict:
    """生效的通道配置：DB 已配置的通道整体覆盖，未配置的通道回退 env"""
    cfg = {
        "wecom_enabled": bool(settings.wecom_webhook),
        "wecom_webhook": settings.wecom_webhook,
        "smtp_enabled": bool(settings.smtp_host and settings.smtp_user),
        "smtp_host": settings.smtp_host,
        "smtp_port": settings.smtp_port,
        "smtp_ssl": settings.smtp_port == 465,
        "smtp_user": settings.smtp_user,
        "smtp_password": settings.smtp_password,
        "smtp_from": settings.smtp_from,
        "smtp_to": settings.alert_email_to,
    }
    row = _cfg_row()
    if row is None:
        return cfg
    if (row.wecom_webhook or "") or row.wecom_enabled:
        cfg["wecom_enabled"] = bool(row.wecom_enabled)
        cfg["wecom_webhook"] = row.wecom_webhook or ""
    if (row.smtp_host or "") or (row.smtp_user or "") or row.smtp_enabled:
        cfg["smtp_enabled"] = bool(row.smtp_enabled)
        cfg["smtp_host"] = row.smtp_host or ""
        cfg["smtp_port"] = row.smtp_port or 465
        cfg["smtp_ssl"] = bool(row.smtp_ssl)
        cfg["smtp_user"] = row.smtp_user or ""
        cfg["smtp_password"] = row.smtp_password or ""
        cfg["smtp_from"] = row.smtp_from or ""
        cfg["smtp_to"] = row.smtp_to or ""
    return cfg


def get_config(db=None) -> dict:
    """读取通道配置（API 用）：密码掩码；source 标注每通道生效来源（db/env/none）"""
    env = channel_config()
    row = db.get(AlertChannelConfig, 1) if db is not None else None
    cfg = {
        "wecom_enabled": env["wecom_enabled"],
        "wecom_webhook": env["wecom_webhook"],
        "smtp_enabled": env["smtp_enabled"],
        "smtp_host": env["smtp_host"],
        "smtp_port": env["smtp_port"],
        "smtp_ssl": env["smtp_ssl"],
        "smtp_user": env["smtp_user"],
        "smtp_password_set": bool(env["smtp_password"]),
        "smtp_from": env["smtp_from"],
        "smtp_to": env["smtp_to"],
        "wecom_source": "db", "smtp_source": "db",
        "updated_at": str(row.updated_at) if row and row.updated_at else "",
    }
    if row is None:
        cfg["wecom_source"] = "env" if settings.wecom_webhook else "none"
        cfg["smtp_source"] = "env" if (settings.smtp_host and settings.smtp_user) else "none"
    else:
        if not (row.wecom_webhook or row.wecom_enabled):
            cfg["wecom_source"] = "env" if settings.wecom_webhook else "none"
        if not (row.smtp_host or row.smtp_user or row.smtp_enabled):
            cfg["smtp_source"] = "env" if (settings.smtp_host and settings.smtp_user) else "none"
    return cfg


def save_config(db, data: dict) -> dict:
    row = db.get(AlertChannelConfig, 1)
    if row is None:
        row = AlertChannelConfig(id=1)
        db.add(row)
    pw = str(data.get("smtp_password") or "")
    if pw and pw != PW_MASK:
        row.smtp_password = pw
    for k in ("wecom_enabled", "wecom_webhook", "smtp_enabled", "smtp_host",
              "smtp_port", "smtp_ssl", "smtp_user", "smtp_from", "smtp_to"):
        if k in data:
            setattr(row, k, data[k])
    db.commit()
    db.refresh(row)
    return get_config(db)


def _channels(rule, cfg: dict | None = None) -> list[str]:
    """规则指定通道优先；未指定 = 全部已配置通道"""
    cfg = cfg or channel_config()
    ok = {"wecom": bool(cfg["wecom_enabled"] and cfg["wecom_webhook"]),
          "email": bool(cfg["smtp_enabled"] and cfg["smtp_host"] and cfg["smtp_user"])}
    if rule.channels:
        return [c for c in rule.channels if ok.get(c)]
    return [c for c in ("wecom", "email") if ok.get(c)]


def _send_wecom(title: str, body: str, cfg: dict):
    r = requests.post(cfg["wecom_webhook"], json={
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


def _send_email(title: str, body: str, cfg: dict):
    to = [x.strip() for x in (cfg["smtp_to"] or "").split(",") if x.strip()]
    if not to:
        raise RuntimeError("未配置邮件收件人（smtp_to）")
    frm = cfg["smtp_from"] or cfg["smtp_user"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(title, "utf-8")
    msg["From"] = formataddr((str(Header("VerveDeck", "utf-8")), frm))
    msg["To"] = ", ".join(to)
    if cfg["smtp_ssl"]:
        s = smtplib.SMTP_SSL(cfg["smtp_host"], cfg["smtp_port"], timeout=10)
    else:
        s = smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=10)
        s.starttls()
    try:
        s.login(cfg["smtp_user"], cfg["smtp_password"])
        s.sendmail(frm, to, msg.as_string())
    finally:
        s.quit()


def _send_one(ch: str, title: str, body: str, cfg: dict):
    if ch == "wecom":
        _send_wecom(title, body, cfg)
    elif ch == "email":
        _send_email(title, body, cfg)
    else:
        raise ValueError(f"未知通道: {ch}")


def test_channel(db, channel: str) -> dict:
    """按通道发送测试通知（不依赖告警规则）"""
    cfg = channel_config()
    if channel not in ("wecom", "email"):
        return {"ok": False, "channel": channel, "error": "通道需为 wecom 或 email"}
    ok_key = "wecom_webhook" if channel == "wecom" else "smtp_user"
    if not (cfg.get("wecom_enabled" if channel == "wecom" else "smtp_enabled")) \
            or not cfg.get(ok_key):
        return {"ok": False, "channel": channel,
                "error": f"{channel} 通道未启用或未配置完整"}
    ev = AlertEvent(ts=int(time.time()), rule_id=None, level="info",
                    target="test", title="[测试告警] 通知通道测试",
                    detail=f"通道: {channel}\n配置来源: {get_config().get(channel + '_source', '')}")
    db.add(ev)
    db.commit()
    db.refresh(ev)
    try:
        _send_one(channel, f"VerveDeck 告警 [info] {ev.title}",
                  f"目标: {ev.target}\n时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n{ev.detail}", cfg)
        ev.notified = True
        db.commit()
        return {"ok": True, "channel": channel}
    except Exception as e:
        ev.notified = False
        db.commit()
        log.warning("test_channel %s failed: %s", channel, e)
        return {"ok": False, "channel": channel, "error": str(e)[:300]}


def _do_notify(rule, ev) -> bool:
    """同步发送（测试接口用）；生产走 _notify 线程包装。返回是否任一通道成功"""
    cfg = channel_config()
    title = f"VerveDeck 告警 [{ev.level}] {ev.title}"
    body = (f"目标: {ev.target}\n"
            f"时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            + (ev.detail or ""))
    sent = False
    for ch in _channels(rule, cfg):
        try:
            _send_one(ch, title, body, cfg)
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
    chs = _channels(rule, channel_config())
    if not chs:
        return {"ok": False, "error": "无可用通知通道（请在「通知通道」配置企业微信或邮件并启用）"}
    ev = AlertEvent(ts=int(time.time()), rule_id=rule.id, level="info",
                    target="test", title="[测试告警] 通知通道测试",
                    detail=f"规则: {rule.name}\n通道: {', '.join(chs)}")
    db.add(ev)
    db.commit()
    db.refresh(ev)
    ok = _do_notify(rule, ev)
    return {"ok": ok, "channels": chs, "notified": ok}

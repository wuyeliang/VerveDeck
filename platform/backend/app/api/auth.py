import ipaddress
import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..security import get_current_user, verify_password, create_token, hash_password
from ..models import User, LoginSecurityConfig
from ..iputil import client_ip

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---- 登录安全（参数可在 系统管理→设置 配置，未配置用默认值） ----
# 防暴力破解：连续失败 → 锁定（用户名与 IP 双维度，阈值不同）
# - 单账号（u:）：默认 5 次失败即锁。这是"防暴破"的核心——锁定对某账号的密码爆破。
# - 单 IP（ip:）：默认 30 次失败才锁。IP 常被 NAT/办公出口/测试并发共享，阈值需更宽松，
#   避免一个正常出口 IP 上的少量失败（或测试套件负例）误伤同出口的其他账号；
#   同时仍能拦截"单 IP 高频尝试大量账号"的横向爆破。
# IP 白名单：启用后仅名单内 IP（支持 CIDR）可登录。
DEFAULT_LOGIN_CFG = {"user_attempts": 5, "ip_attempts": 30, "lock_minutes": 15}
_attempts: dict[str, list[float]] = {}   # key -> 锁定期内的失败时间戳
_locks: dict[str, float] = {}            # key -> 解锁时间戳
_track_lock = threading.Lock()

_cfg_cache: dict = {"ts": 0.0, "cfg": None}  # 配置 10s TTL 缓存（登录非高频，避免每次查 DB）


def load_login_config() -> dict:
    """生效登录安全配置：DB 单行配置优先，缺省用默认值。"""
    now = time.time()
    if _cfg_cache["cfg"] is not None and now - _cfg_cache["ts"] < 10:
        return _cfg_cache["cfg"]
    cfg = dict(DEFAULT_LOGIN_CFG)
    cfg["ip_whitelist_enabled"] = False
    cfg["whitelist"] = []  # [ip_network 对象]
    try:
        db = SessionLocal()
        try:
            row = db.get(LoginSecurityConfig, 1)
            if row:
                cfg["user_attempts"] = max(1, row.user_attempts)
                cfg["ip_attempts"] = max(1, row.ip_attempts)
                cfg["lock_minutes"] = max(1, row.lock_minutes)
                cfg["ip_whitelist_enabled"] = bool(row.ip_whitelist_enabled)
                nets = []
                for tok in (row.ip_whitelist or "").replace("\n", ",").replace(" ", ",").split(","):
                    tok = tok.strip()
                    if not tok:
                        continue
                    try:
                        nets.append(ipaddress.ip_network(tok, strict=False))
                    except ValueError:
                        continue
                cfg["whitelist"] = nets
        finally:
            db.close()
    except Exception:
        pass  # DB 不可用时回退默认
    _cfg_cache.update(ts=now, cfg=cfg)
    return cfg


def invalidate_login_config_cache():
    _cfg_cache.update(ts=0.0, cfg=None)


def _threshold(key: str, cfg: dict) -> int:
    return cfg["ip_attempts"] if key.startswith("ip:") else cfg["user_attempts"]


def _check_locked(keys: list[str], cfg: dict) -> int:
    """返回剩余锁定秒数（0=未锁定）"""
    now = time.time()
    with _track_lock:
        for k in keys:
            until = _locks.get(k, 0)
            if until > now:
                return int(until - now) + 1
    return 0


def _record_failure(keys: list[str], cfg: dict) -> bool:
    """记录失败；达到对应维度阈值则上锁。返回是否已触发锁定"""
    now = time.time()
    lock_s = cfg["lock_minutes"] * 60
    locked = False
    with _track_lock:
        for k in keys:
            ts = [t for t in _attempts.get(k, []) if now - t < lock_s]
            ts.append(now)
            _attempts[k] = ts
            if len(ts) >= _threshold(k, cfg):
                _locks[k] = now + lock_s
                _attempts[k] = []
                locked = True
    return locked


def _clear_failures(key: str):
    with _track_lock:
        _attempts.pop(key, None)
        _locks.pop(key, None)


def _remaining(key: str, cfg: dict) -> int:
    with _track_lock:
        return max(0, _threshold(key, cfg) - len(_attempts.get(key, [])))


def _fire_login_alert(db, username: str, ip: str):
    """锁定触发 → 安全告警（复用事件型规则机制：系统管理→告警 可配阈值/通道/开关）。
    被规则 cooldown 去重（默认 300s 同 IP 不重复轰炸）；通知通道未配置时仅入库。"""
    try:
        from ..services import alert_service
        alert_service.trigger_event(
            db, "login_locked", target=f"login:{ip}",
            title=f"登录锁定（疑似暴力破解）: {username or '未知账号'}",
            detail=f"来源 IP: {ip}\n账号: {username or '-'}\n"
                   f"触发: 连续失败达到锁定阈值，账号与 IP 已临时锁定")
    except Exception:
        pass  # 告警失败不影响登录主流程


class LoginReq(BaseModel):
    username: str
    password: str


class PwReq(BaseModel):
    old_password: str
    new_password: str


@router.post("/login")
def login(req: LoginReq, request: Request, db: Session = Depends(get_db)):
    cfg = load_login_config()
    ip = client_ip(request)
    # IP 白名单（启用后仅名单内可登录；不透露具体策略防探测）
    if cfg["ip_whitelist_enabled"] and cfg["whitelist"]:
        try:
            ip_addr = ipaddress.ip_address(ip)
        except ValueError:
            ip_addr = None
        if ip_addr is None or not any(ip_addr in n for n in cfg["whitelist"]):
            raise HTTPException(403, "当前网络环境不允许登录")
    ukey, ikey = f"u:{req.username}", f"ip:{ip}"
    rem = _check_locked([ukey, ikey], cfg)
    if rem:
        raise HTTPException(423, f"登录失败次数过多，已临时锁定，请 {rem} 秒后重试")
    user = db.query(User).filter_by(username=req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        locked = _record_failure([ukey, ikey], cfg)
        if locked:
            _fire_login_alert(db, req.username, ip)
            raise HTTPException(423, f"登录失败次数过多，账号已临时锁定 {cfg['lock_minutes']} 分钟")
        raise HTTPException(401, f"用户名或密码错误（还剩 {_remaining(ukey, cfg)} 次机会）")
    if not user.is_active:
        raise HTTPException(403, "账号已禁用")
    _clear_failures(ukey)
    return {"token": create_token(user), "user": _user_brief(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return _user_brief(user)


@router.put("/password")
def change_password(req: PwReq, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(req.old_password, user.password_hash):
        raise HTTPException(400, "原密码错误")
    if req.new_password == req.old_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    if len(req.new_password) < 8:
        raise HTTPException(400, "新密码至少 8 位（公网部署建议含大小写与数字）")
    user.password_hash = hash_password(req.new_password)
    user.must_change_password = False  # 改密后解除首登强制
    db.commit()
    return {"ok": True}


def _user_brief(u: User):
    return {"id": u.id, "username": u.username, "full_name": u.full_name,
            "role": u.role, "tenant_id": u.tenant_id,
            "must_change_password": bool(u.must_change_password)}

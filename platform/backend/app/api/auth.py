import threading
import time

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..security import get_current_user, verify_password, create_token, hash_password
from ..models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])

# ---- 登录防暴力破解：连续失败 → 锁定 15 分钟（用户名与 IP 双维度，阈值不同） ----
# - 单账号（u:）：5 次失败即锁。这是"防暴破"的核心——锁定对某账号的密码爆破。
# - 单 IP（ip:）：30 次失败才锁。IP 常被 NAT/办公出口/测试并发共享，阈值需更宽松，
#   避免一个正常出口 IP 上的少量失败（或测试套件负例）误伤同出口的其他账号；
#   同时仍能拦截"单 IP 高频尝试大量账号"的横向爆破。
MAX_USER_ATTEMPTS = 5
MAX_IP_ATTEMPTS = 30
LOCK_SECONDS = 15 * 60
_attempts: dict[str, list[float]] = {}   # key -> 锁定期内的失败时间戳
_locks: dict[str, float] = {}            # key -> 解锁时间戳
_track_lock = threading.Lock()


def _threshold(key: str) -> int:
    return MAX_IP_ATTEMPTS if key.startswith("ip:") else MAX_USER_ATTEMPTS


def _check_locked(keys: list[str]) -> int:
    """返回剩余锁定秒数（0=未锁定）"""
    now = time.time()
    with _track_lock:
        for k in keys:
            until = _locks.get(k, 0)
            if until > now:
                return int(until - now) + 1
    return 0


def _record_failure(keys: list[str]) -> bool:
    """记录失败；达到对应维度阈值则上锁。返回是否已触发锁定"""
    now = time.time()
    locked = False
    with _track_lock:
        for k in keys:
            ts = [t for t in _attempts.get(k, []) if now - t < LOCK_SECONDS]
            ts.append(now)
            _attempts[k] = ts
            if len(ts) >= _threshold(k):
                _locks[k] = now + LOCK_SECONDS
                _attempts[k] = []
                locked = True
    return locked


def _clear_failures(key: str):
    with _track_lock:
        _attempts.pop(key, None)
        _locks.pop(key, None)


def _remaining(key: str) -> int:
    with _track_lock:
        return max(0, _threshold(key) - len(_attempts.get(key, [])))


class LoginReq(BaseModel):
    username: str
    password: str


class PwReq(BaseModel):
    old_password: str
    new_password: str


@router.post("/login")
def login(req: LoginReq, request: Request, db: Session = Depends(get_db)):
    ip = request.client.host if request.client else "unknown"
    ukey, ikey = f"u:{req.username}", f"ip:{ip}"
    rem = _check_locked([ukey, ikey])
    if rem:
        raise HTTPException(423, f"登录失败次数过多，已临时锁定，请 {rem} 秒后重试")
    user = db.query(User).filter_by(username=req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        locked = _record_failure([ukey, ikey])
        if locked:
            raise HTTPException(423, f"登录失败次数过多，账号已临时锁定 {LOCK_SECONDS // 60} 分钟")
        raise HTTPException(401, f"用户名或密码错误（还剩 {_remaining(ukey)} 次机会）")
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
    if len(req.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    user.password_hash = hash_password(req.new_password)
    user.must_change_password = False  # 改密后解除首登强制
    db.commit()
    return {"ok": True}


def _user_brief(u: User):
    return {"id": u.id, "username": u.username, "full_name": u.full_name,
            "role": u.role, "tenant_id": u.tenant_id,
            "must_change_password": bool(u.must_change_password)}

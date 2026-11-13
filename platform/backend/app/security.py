"""认证与 RBAC：JWT 登录、角色校验、项目空间访问控制、API Key 校验"""
import secrets
import hashlib
from datetime import datetime, timedelta

import bcrypt
import jwt
from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from .config import settings
from .database import get_db
from .models import User, Project, ProjectMember, ApiKey

_bearer = HTTPBearer(auto_error=False)

ROLE_LEVEL = {"super_admin": 100, "tenant_admin": 80, "developer": 50, "viewer": 30}


def hash_password(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def verify_password(pw: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except Exception:
        return False


def create_token(user: User) -> str:
    payload = {
        "sub": str(user.id), "username": user.username, "role": user.role,
        "exp": datetime.utcnow() + timedelta(hours=settings.jwt_expire_hours),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm="HS256")


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "token 已过期")
    except jwt.InvalidTokenError:
        raise HTTPException(401, "无效 token")


def get_current_user(request: Request, db: Session = Depends(get_db),
                     cred: HTTPAuthorizationCredentials | None = Depends(_bearer)) -> User:
    if not cred:
        raise HTTPException(401, "未登录")
    payload = decode_token(cred.credentials)
    user = db.get(User, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(401, "用户不存在或已禁用")
    request.state.user = user
    return user


def require_role(*roles: str):
    def checker(user: User = Depends(get_current_user)):
        if user.role not in roles and user.role != "super_admin":
            raise HTTPException(403, f"需要角色: {', '.join(roles)}")
        return user
    return checker


def can_access_project(user: User, db: Session, project: Project) -> bool:
    if user.role == "super_admin":
        return True
    if user.tenant_id is None or project.tenant_id != user.tenant_id:
        return False
    if project.owner_id == user.id:
        return True
    return db.query(ProjectMember).filter_by(project_id=project.id, user_id=user.id).first() is not None


def get_project_or_404(db: Session, project_id: int, user: User) -> Project:
    proj = db.get(Project, project_id)
    if not proj:
        raise HTTPException(404, "项目不存在")
    if not can_access_project(user, db, proj):
        raise HTTPException(403, "无该项目访问权限")
    return proj


def verify_api_key(db: Session, project_id: int, key: str) -> ApiKey | None:
    """校验 OpenAI 代理的 x-api-key（项目级 key）"""
    if not key:
        return None
    h = hashlib.sha256(key.encode()).hexdigest()
    k = db.query(ApiKey).filter_by(project_id=project_id, key_hash=h, is_active=True).first()
    return k


def new_api_key() -> tuple[str, str]:
    raw = "ofk_" + secrets.token_hex(24)
    return raw, hashlib.sha256(raw.encode()).hexdigest()

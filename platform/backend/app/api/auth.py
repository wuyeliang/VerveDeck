from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..security import get_current_user, verify_password, create_token, hash_password
from ..models import User

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginReq(BaseModel):
    username: str
    password: str


class PwReq(BaseModel):
    old_password: str
    new_password: str


@router.post("/login")
def login(req: LoginReq, db: Session = Depends(get_db)):
    user = db.query(User).filter_by(username=req.username).first()
    if not user or not verify_password(req.password, user.password_hash):
        raise HTTPException(401, "用户名或密码错误")
    if not user.is_active:
        raise HTTPException(403, "账号已禁用")
    return {"token": create_token(user), "user": _user_brief(user)}


@router.get("/me")
def me(user: User = Depends(get_current_user)):
    return _user_brief(user)


@router.put("/password")
def change_password(req: PwReq, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not verify_password(req.old_password, user.password_hash):
        raise HTTPException(400, "原密码错误")
    user.password_hash = hash_password(req.new_password)
    db.commit()
    return {"ok": True}


def _user_brief(u: User):
    return {"id": u.id, "username": u.username, "full_name": u.full_name,
            "role": u.role, "tenant_id": u.tenant_id}

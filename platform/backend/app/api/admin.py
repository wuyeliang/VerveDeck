"""系统管理：租户/项目/用户(RBAC)/API Key/护栏规则/审计日志"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
import time

from ..database import get_db
from ..security import get_current_user, require_role, hash_password, new_api_key, can_access_project
from ..models import (Tenant, User, Project, ProjectMember, ApiKey, GuardRule,
                      AuditLog, Dataset)

router = APIRouter(prefix="/api/admin", tags=["admin"])


# ---------- 租户 ----------

class TenantReq(BaseModel):
    name: str
    description: str = ""


@router.get("/tenants")
def list_tenants(db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin"))):
    q = db.query(Tenant)
    if user.role != "super_admin" and user.tenant_id:
        q = q.filter(Tenant.id == user.tenant_id)
    return [t for t in q.all()]


@router.post("/tenants")
def create_tenant(req: TenantReq, db=Depends(get_db), user=Depends(require_role("super_admin"))):
    if db.query(Tenant).filter_by(name=req.name).first():
        raise HTTPException(400, "租户名已存在")
    t = Tenant(name=req.name, created_at=None)
    db.add(t)
    db.commit()
    db.refresh(t)
    return t


# ---------- 项目 ----------

class ProjectReq(BaseModel):
    tenant_id: int
    name: str
    description: str = ""


@router.get("/projects")
def list_projects(db=Depends(get_db), user=Depends(get_current_user)):
    if user.role == "super_admin":
        projs = db.query(Project).all()
    else:
        projs = db.query(Project).filter(Project.tenant_id == user.tenant_id).all()
    return [{
        "id": p.id, "tenant_id": p.tenant_id, "name": p.name,
        "description": p.description, "owner_id": p.owner_id,
        "created_at": str(p.created_at),
        "can_access": can_access_project(user, db, p),
    } for p in projs]


@router.post("/projects")
def create_project(req: ProjectReq, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin"))):
    tenant = db.get(Tenant, req.tenant_id)
    if not tenant:
        raise HTTPException(404, "租户不存在")
    if db.query(Project).filter_by(tenant_id=req.tenant_id, name=req.name).first():
        raise HTTPException(400, "项目名已存在")
    p = Project(tenant_id=req.tenant_id, name=req.name, description=req.description, owner_id=user.id)
    db.add(p)
    db.flush()
    db.add(ProjectMember(project_id=p.id, user_id=user.id, role="owner"))
    db.commit()
    db.refresh(p)
    return p


@router.put("/projects/{pid}/members")
def add_member(pid: int, req: dict, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin"))):
    proj = db.get(Project, pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    if user.role != "super_admin" and user.tenant_id != proj.tenant_id:
        raise HTTPException(403, "无权操作")
    try:
        uid = int(req.get("user_id"))  # 兼容数字字符串
    except (TypeError, ValueError):
        raise HTTPException(400, "user_id 必填且须为整数")
    m = db.query(ProjectMember).filter_by(project_id=pid, user_id=uid).first()
    if m:  # 幂等：重复添加视为更新角色
        if "role" in req:
            m.role = req["role"]
        db.commit()
        return {"ok": True}
    db.add(ProjectMember(project_id=pid, user_id=uid, role=req.get("role", "developer")))
    db.commit()
    return {"ok": True}


@router.delete("/projects/{pid}/members/{uid}")
def remove_member(pid: int, uid: int, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin"))):
    db.query(ProjectMember).filter_by(project_id=pid, user_id=uid).delete()
    db.commit()
    return {"ok": True}


# ---------- 用户 ----------

class UserReq(BaseModel):
    username: str
    password: str
    full_name: str = ""
    role: str = "developer"
    tenant_id: int | None = None


@router.get("/users")
def list_users(db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin"))):
    q = db.query(User)
    if user.role != "super_admin":
        q = q.filter(User.tenant_id == user.tenant_id)
    return [{"id": u.id, "username": u.username, "full_name": u.full_name,
             "role": u.role, "tenant_id": u.tenant_id, "is_active": u.is_active,
             "created_at": str(u.created_at)} for u in q.all()]


@router.post("/users")
def create_user(req: UserReq, db=Depends(get_db), user=Depends(require_role("super_admin"))):
    if db.query(User).filter_by(username=req.username).first():
        raise HTTPException(400, "用户名已存在")
    u = User(username=req.username, password_hash=hash_password(req.password),
             full_name=req.full_name, role=req.role, tenant_id=req.tenant_id)
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"id": u.id, "username": u.username, "role": u.role}


@router.put("/users/{uid}")
def update_user(uid: int, req: dict, db=Depends(get_db), user=Depends(require_role("super_admin"))):
    u = db.get(User, uid)
    if not u:
        raise HTTPException(404, "用户不存在")
    for k in ("full_name", "role", "is_active", "tenant_id"):
        if k in req:
            setattr(u, k, req[k])
    if "password" in req and req["password"]:
        u.password_hash = hash_password(req["password"])
    db.commit()
    return {"ok": True}


# ---------- API Key ----------

@router.get("/projects/{pid}/api-keys")
def list_keys(pid: int, db=Depends(get_db), user=Depends(get_current_user)):
    proj = db.get(Project, pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    if not can_access_project(user, db, proj):
        raise HTTPException(403, "无权限")
    return [{"id": k.id, "name": k.name, "key_prefix": k.key_prefix,
             "is_active": k.is_active, "created_at": str(k.created_at)} for k in
            db.query(ApiKey).filter_by(project_id=pid).all()]


@router.post("/projects/{pid}/api-keys")
def create_key(pid: int, req: dict, db=Depends(get_db), user=Depends(get_current_user)):
    proj = db.get(Project, pid)
    if not proj:
        raise HTTPException(404, "项目不存在")
    if not can_access_project(user, db, proj):
        raise HTTPException(403, "无权限")
    raw, h = new_api_key()
    k = ApiKey(project_id=pid, name=req.get("name", "default"), key_hash=h, key_prefix=raw[:12])
    db.add(k)
    db.commit()
    return {"key": raw, "name": k.name, "prefix": raw[:12]}  # key 只展示一次


@router.delete("/projects/{pid}/api-keys/{kid}")
def delete_key(pid: int, kid: int, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin", "developer"))):
    k = db.get(ApiKey, kid)
    if k:
        k.is_active = False
        db.commit()
    return {"ok": True}


# ---------- 护栏规则 ----------

@router.get("/guard-rules")
def list_rules(project_id: int | None = None, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin"))):
    q = db.query(GuardRule)
    if project_id is not None:
        q = q.filter((GuardRule.project_id == project_id) | (GuardRule.project_id.is_(None)))
    return [dict(id=r.id, project_id=r.project_id, pattern=r.pattern, scope=r.scope,
                 description=r.description, enabled=r.enabled) for r in q.all()]


@router.post("/guard-rules")
def create_rule(req: dict, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin"))):
    pattern = req.get("pattern")
    if not pattern:
        raise HTTPException(400, "pattern 必填")
    r = GuardRule(project_id=req.get("project_id"), pattern=pattern,
                  scope=req.get("scope", "both"), description=req.get("description", ""),
                  enabled=req.get("enabled", True))
    db.add(r)
    db.commit()
    return {"id": r.id}


@router.delete("/guard-rules/{rid}")
def delete_rule(rid: int, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin"))):
    db.query(GuardRule).filter_by(id=rid).delete()
    db.commit()
    return {"ok": True}


# ---------- 审计日志 ----------

@router.get("/audit")
def audit_logs(limit: int = 100, offset: int = 0, username: str = "",
               db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin"))):
    q = db.query(AuditLog)
    if username:
        q = q.filter(AuditLog.username == username)
    total = q.count()
    rows = (q.order_by(AuditLog.id.desc()).offset(offset).limit(min(limit, 500)).all())
    return {"total": total, "items": [
        {"id": r.id, "ts": r.ts, "username": r.username, "method": r.method,
         "path": r.path, "ip": r.ip, "status_code": r.status_code, "detail": r.detail}
        for r in rows]}

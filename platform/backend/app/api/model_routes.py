"""模型路由 CRUD：服务名 → 多部署目标（权重 + 容灾）。

数据面由 Higress 网关承载（路径 /v1/<name>/），本模块只管理期望态；
reconciler（higress_sync.sync_routes，调度器 tick 驱动）把目标权重/
容灾接管/重试策略收敛到网关 Console。写入后 sync_status 置 pending，
下一 tick 收敛为 synced（或 error，含原因）。

命名约束：路由名与部署名全局互斥（/v1/<名>/ 前缀不能碰撞）——
路由侧校验在本模块，部署侧校验在 inference.update_deploy。
"""
import logging
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..security import get_current_user, can_access_project
from ..models import Deployment, ModelRoute, ModelRouteTarget, Project

log = logging.getLogger("model-routes")
router = APIRouter(tags=["model-routes"])

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")


# ---------- 校验 / 序列化 ----------

def _check_project(db, pid, user):
    p = db.get(Project, pid)
    if not p or not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")


def _validate_route_name(db, name: str, exclude_id: int | None = None) -> str:
    name = (name or "").strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(400, "路由名须为小写字母/数字/连字符（1-128，字母或数字开头）")
    q = db.query(ModelRoute).filter_by(name=name)
    if exclude_id:
        q = q.filter(ModelRoute.id != exclude_id)
    if q.first():
        raise HTTPException(400, "路由名已存在")
    if db.query(Deployment).filter_by(name=name).first():
        raise HTTPException(400, "与已有部署名冲突（/v1/ 路径前缀不能碰撞）")
    return name


def _check_weight(w) -> int:
    try:
        w = int(w)
    except (TypeError, ValueError):
        raise HTTPException(400, "权重须为整数")
    if not (1 <= w <= 100):
        raise HTTPException(400, "权重须为 1-100（相对值，reconciler 归一化到 100）")
    return w


def _get_dep(db, deployment_id: int, user) -> Deployment:
    dep = db.get(Deployment, deployment_id)
    if not dep:
        raise HTTPException(400, f"部署 {deployment_id} 不存在")
    _check_project(db, dep.project_id, user)
    return dep


def _targets_of(db, route_id: int) -> list:
    return (db.query(ModelRouteTarget)
            .filter_by(route_id=route_id)
            .order_by(ModelRouteTarget.id).all())


def _target_brief(t: ModelRouteTarget, dep: Deployment | None) -> dict:
    return {
        "id": t.id, "deployment_id": t.deployment_id,
        "deployment_name": dep.name if dep else "",
        "deployment_status": dep.status if dep else "deleted",
        "weight": t.weight, "is_fallback": bool(t.is_fallback),
    }


def _route_brief(r: ModelRoute, db) -> dict:
    deps = {}
    targets = _targets_of(db, r.id)
    dep_ids = {t.deployment_id for t in targets}
    if dep_ids:
        deps = {d.id: d for d in db.query(Deployment).filter(Deployment.id.in_(dep_ids)).all()}
    return {
        "id": r.id, "name": r.name, "project_id": r.project_id,
        "description": r.description or "",
        "enabled": bool(r.enabled),
        "sync_status": r.sync_status or "pending",
        "sync_message": r.sync_message or "",
        "endpoint": f"/v1/{r.name}/",
        "created_at": str(r.created_at),
        "targets": [_target_brief(t, deps.get(t.deployment_id)) for t in targets],
    }


def _get_route(db, rid: int, user) -> ModelRoute:
    r = db.get(ModelRoute, rid)
    if not r:
        raise HTTPException(404, "路由不存在")
    _check_project(db, r.project_id, user)
    return r


def _mark_pending(r: ModelRoute) -> None:
    r.sync_status = "pending"
    r.sync_message = "待同步（下一周期生效）"


def _apply_targets(db, r: ModelRoute, items: list, user) -> None:
    """全量替换目标集（编辑语义）：校验后删旧建新。
    校验：部署存在且可访问 / 同路由同部署唯一 / 权重 1-100。"""
    seen: set[int] = set()
    cleaned: list[tuple[Deployment, int, bool]] = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            raise HTTPException(400, f"第 {i + 1} 个目标须为对象")
        dep = _get_dep(db, int(it.get("deployment_id") or 0), user)
        if dep.id in seen:
            raise HTTPException(400, f"部署 {dep.name} 在目标中重复（同一部署不可同时为主目标与容灾目标）")
        seen.add(dep.id)
        cleaned.append((dep, _check_weight(it.get("weight", 1)), bool(it.get("is_fallback", False))))
    db.query(ModelRouteTarget).filter_by(route_id=r.id).delete()
    for dep, w, fb in cleaned:
        db.add(ModelRouteTarget(route_id=r.id, deployment_id=dep.id,
                                weight=w, is_fallback=fb))


# ---------- 路由 CRUD ----------

class TargetIn(BaseModel):
    deployment_id: int
    weight: int = 1
    is_fallback: bool = False


class RouteReq(BaseModel):
    project_id: int
    name: str
    description: str = ""
    targets: list[TargetIn] = []


@router.get("/api/inference/routes")
def list_routes(project_id: int | None = None, db: Session = Depends(get_db),
                user=Depends(get_current_user)):
    if project_id:
        _check_project(db, project_id, user)
        q = db.query(ModelRoute).filter(ModelRoute.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = db.query(ModelRoute).filter(ModelRoute.project_id.in_(pids or [0]))
    return [_route_brief(r, db) for r in q.order_by(ModelRoute.id.desc()).all()]


@router.post("/api/inference/routes")
def create_route(req: RouteReq, db: Session = Depends(get_db), user=Depends(get_current_user)):
    _check_project(db, req.project_id, user)
    name = _validate_route_name(db, req.name)
    r = ModelRoute(project_id=req.project_id, name=name,
                   description=req.description or "", created_by=user.id)
    db.add(r)
    db.flush()
    _apply_targets(db, r, [t.model_dump() for t in req.targets], user)
    _mark_pending(r)
    db.commit()
    db.refresh(r)
    return _route_brief(r, db)


@router.get("/api/inference/routes/{rid}")
def get_route(rid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _route_brief(_get_route(db, rid, user), db)


@router.put("/api/inference/routes/{rid}")
def update_route(rid: int, req: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """编辑：name/description/enabled/targets（目标全量替换）。"""
    r = _get_route(db, rid, user)
    req = {k: v for k, v in (req or {}).items() if v is not None or k == "enabled"}
    if not req:
        raise HTTPException(400, "无更新字段")
    if "name" in req and req["name"] != r.name:
        r.name = _validate_route_name(db, req["name"], exclude_id=r.id)
    if "description" in req:
        r.description = req["description"]
    if "enabled" in req:
        r.enabled = bool(req["enabled"])
    if "targets" in req:
        _apply_targets(db, r, req["targets"], user)
    _mark_pending(r)
    db.commit()
    db.refresh(r)
    return _route_brief(r, db)


@router.delete("/api/inference/routes/{rid}")
def delete_route(rid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    r = _get_route(db, rid, user)
    db.query(ModelRouteTarget).filter_by(route_id=r.id).delete()
    db.delete(r)
    db.commit()
    # 网关侧残留由 reconciler 孤儿清理（vd- 前缀）在下个 tick 摘除
    return {"ok": True}


# ---------- 目标管理（单条增删改，前端行内编辑用） ----------

@router.post("/api/inference/routes/{rid}/targets")
def add_target(rid: int, req: TargetIn, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    r = _get_route(db, rid, user)
    dep = _get_dep(db, req.deployment_id, user)
    exists = db.query(ModelRouteTarget).filter_by(route_id=r.id,
                                                  deployment_id=dep.id).first()
    if exists:
        raise HTTPException(400, f"部署 {dep.name} 已在此路由中（可先修改权重/容灾标记）")
    t = ModelRouteTarget(route_id=r.id, deployment_id=dep.id,
                         weight=_check_weight(req.weight),
                         is_fallback=bool(req.is_fallback))
    db.add(t)
    _mark_pending(r)
    db.commit()
    return _route_brief(r, db)


@router.put("/api/inference/routes/{rid}/targets/{tid}")
def update_target(rid: int, tid: int, req: dict, db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    r = _get_route(db, rid, user)
    t = db.get(ModelRouteTarget, tid)
    if not t or t.route_id != rid:
        raise HTTPException(404, "目标不存在")
    req = {k: v for k, v in (req or {}).items() if v is not None}
    if not req:
        raise HTTPException(400, "无更新字段")
    if "weight" in req:
        t.weight = _check_weight(req["weight"])
    if "is_fallback" in req:
        t.is_fallback = bool(req["is_fallback"])
    _mark_pending(r)
    db.commit()
    return _route_brief(r, db)


@router.delete("/api/inference/routes/{rid}/targets/{tid}")
def delete_target(rid: int, tid: int, db: Session = Depends(get_db),
                  user=Depends(get_current_user)):
    r = _get_route(db, rid, user)
    t = db.get(ModelRouteTarget, tid)
    if not t or t.route_id != rid:
        raise HTTPException(404, "目标不存在")
    db.delete(t)
    _mark_pending(r)
    db.commit()
    return _route_brief(r, db)

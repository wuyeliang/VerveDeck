"""模型路由 CRUD：服务名 → 多部署目标（负载均衡权重 / 优先级级联）。

数据面由 Higress 网关承载（路径 /v1/<name>/），本模块只管理期望态；
reconciler（higress_sync.sync_routes，调度器 tick 驱动）把目标权重/
优先级接管/重试策略收敛到网关 Console。写入后 sync_status 置 pending，
下一 tick 收敛为 synced（或 error，含原因）。

路由策略：
- lb（负载均衡）：主目标按权重分发；主目标全不可用时 fallback 接管；
- priority（优先级路由）：目标按 priority 升序级联，第一个 running 独占
  全部流量（权重 100），其不可用自动下移，恢复自动回切。

部署创建时自动生成同名路由（ensure_auto_route，auto=True），删除时 GC
（gc_auto_route_for）；路由名与部署名允许相同（网关最长前缀匹配，具体
路由优先于 /v1/ 兜底直连，同名时语义一致）。
"""
import logging
import re
import threading

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from ..database import get_db
from ..security import get_current_user, can_access_project
from ..models import CacheService, Deployment, ModelRoute, ModelRouteTarget, Project

log = logging.getLogger("model-routes")
router = APIRouter(tags=["model-routes"])

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,127}$")
_POLICIES = ("lb", "priority")


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
    return name


def _check_policy(p) -> str:
    p = (p or "lb").strip()
    if p not in _POLICIES:
        raise HTTPException(400, "路由策略须为 lb（负载均衡）/ priority（优先级路由）")
    return p


def _check_cache(db, cache_service_id) -> int | None:
    if not cache_service_id:
        return None
    if not db.get(CacheService, cache_service_id):
        raise HTTPException(400, f"缓存服务 {cache_service_id} 不存在")
    return int(cache_service_id)


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


def _targets_of(db, route_id: int, policy: str = "lb") -> list:
    """目标列表：priority 策略按优先级序号（编辑态有序展示），lb 按入库序。"""
    q = db.query(ModelRouteTarget).filter_by(route_id=route_id)
    if (policy or "lb") == "priority":
        q = q.order_by(ModelRouteTarget.priority, ModelRouteTarget.id)
    else:
        q = q.order_by(ModelRouteTarget.id)
    return q.all()


def _target_brief(t: ModelRouteTarget, dep: Deployment | None) -> dict:
    return {
        "id": t.id, "deployment_id": t.deployment_id,
        "deployment_name": dep.name if dep else "",
        "deployment_status": dep.status if dep else "deleted",
        "weight": t.weight, "is_fallback": bool(t.is_fallback),
        "priority": t.priority or 1,
    }


def _route_brief(r: ModelRoute, db) -> dict:
    deps = {}
    targets = _targets_of(db, r.id, r.policy)
    dep_ids = {t.deployment_id for t in targets}
    if dep_ids:
        deps = {d.id: d for d in db.query(Deployment).filter(Deployment.id.in_(dep_ids)).all()}
    cache_name = ""
    if r.cache_service_id:
        c = db.get(CacheService, r.cache_service_id)
        cache_name = c.name if c else ""
    return {
        "id": r.id, "name": r.name, "project_id": r.project_id,
        "description": r.description or "",
        "enabled": bool(r.enabled),
        "policy": r.policy or "lb",
        "cache_service_id": r.cache_service_id,
        "cache_name": cache_name,
        "auto": bool(r.auto),
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


# 按部署串行化「stop+launch」：连续两次缓存变更若不加锁，两次请求的
# stop/launch 会交错（第二次 stop 删掉第一次刚建的容器、两次创建并发撞名），
# 曾导致常驻部署 409 → failed。启动线程仍由令牌作废收敛，此处只防请求侧交错。
_restart_locks: dict[int, threading.Lock] = {}
_restart_locks_guard = threading.Lock()


def _restart_lock(did: int) -> threading.Lock:
    with _restart_locks_guard:
        return _restart_locks.setdefault(did, threading.Lock())


def _restart_targets(db, r: ModelRoute) -> None:
    """路由级缓存变更 → 重启运行中的目标部署（容器重建时重新注入 env）。

    stopped 目标下次启动自然取新值，无需处理；单目标失败不影响其他。"""
    from ..services import inference_service as inf
    targets = _targets_of(db, r.id, r.policy)
    dep_ids = {t.deployment_id for t in targets}
    if not dep_ids:
        return
    deps = {d.id: d for d in db.query(Deployment).filter(Deployment.id.in_(dep_ids)).all()}
    for d in deps.values():
        if d.status not in ("running", "creating"):
            continue
        with _restart_lock(d.id):
            try:
                inf.stop(d)  # 换发启动令牌 + 按名称/标签清扫实例
                d.status = "creating"
                d.state_message = "路由缓存变更，重建实例中…"
                db.commit()
                inf.launch_thread(d.id, note="路由缓存变更，重建实例中…")
            except Exception as e:  # noqa: BLE001  单目标失败不阻塞路由保存
                log.warning("restart target %s failed: %s", d.name, e)
                try:
                    db.rollback()
                except Exception:
                    pass


# ---------- 部署自动关联路由 ----------

def ensure_auto_route(db, dep: Deployment, user_id: int) -> bool:
    """部署创建 → 自动同名路由（auto=True，部署为唯一目标）。

    幂等：已有同名路由（人工建的）→ 仅确保该部署在目标中（追加末位）。
    返回是否发生变更（调用方决定 commit）。"""
    r = db.query(ModelRoute).filter_by(name=dep.name).first()
    changed = False
    if r is None:
        r = ModelRoute(project_id=dep.project_id, name=dep.name,
                       description="", policy="lb", auto=True, created_by=user_id)
        db.add(r)
        db.flush()
        changed = True
    if not db.query(ModelRouteTarget).filter_by(route_id=r.id,
                                                deployment_id=dep.id).first():
        nxt = (db.query(func.max(ModelRouteTarget.priority))
               .filter_by(route_id=r.id).scalar() or 0) + 1
        n_targets = db.query(func.count(ModelRouteTarget.id)) \
            .filter_by(route_id=r.id).scalar() or 0
        db.add(ModelRouteTarget(route_id=r.id, deployment_id=dep.id,
                                weight=100,
                                is_fallback=(r.policy == "priority" and n_targets > 0),
                                priority=nxt))
        changed = True
    if changed:
        _mark_pending(r)
    return changed


def gc_auto_route_for(db, dep_name: str, dep_id: int) -> None:
    """部署删除 → 自动路由 GC（调用前部署行已删、DB 级联已清目标）。

    auto 路由且目标已空 → 删路由；人工加过其他目标 → 保留（仅失去该目标）。"""
    r = db.query(ModelRoute).filter_by(name=dep_name, auto=True).first()
    if not r:
        return
    left = db.query(func.count(ModelRouteTarget.id)) \
        .filter_by(route_id=r.id).scalar() or 0
    if left == 0:
        db.delete(r)
        log.info("auto route gc: %s (deployment deleted)", dep_name)


def _apply_targets(db, r: ModelRoute, items: list, user, policy: str = "lb") -> None:
    """全量替换目标集（编辑语义）：校验后删旧建新。
    校验：部署存在且可访问 / 同路由同部署唯一 / 权重 1-100。
    归一化：priority 策略按列表序重排 priority=1..n、权重 100、
    首目标为主其余为容灾；lb 策略保留权重/容灾标记（priority 置 1）。"""
    policy = policy or "lb"
    seen: set[int] = set()
    cleaned: list[tuple[Deployment, int, bool]] = []
    for i, it in enumerate(items or []):
        if not isinstance(it, dict):
            raise HTTPException(400, f"第 {i + 1} 个目标须为对象")
        dep = _get_dep(db, int(it.get("deployment_id") or 0), user)
        if dep.id in seen:
            raise HTTPException(400, f"部署 {dep.name} 在目标中重复（同一部署不可同时为主目标与容灾目标）")
        seen.add(dep.id)
        if policy == "priority":
            cleaned.append((dep, 100, i > 0))
        else:
            cleaned.append((dep, _check_weight(it.get("weight", 1)),
                            bool(it.get("is_fallback", False))))
    db.query(ModelRouteTarget).filter_by(route_id=r.id).delete()
    for i, (dep, w, fb) in enumerate(cleaned):
        db.add(ModelRouteTarget(route_id=r.id, deployment_id=dep.id,
                                weight=w, is_fallback=fb, priority=i + 1))


# ---------- 路由 CRUD ----------

class TargetIn(BaseModel):
    deployment_id: int
    weight: int = 1
    is_fallback: bool = False
    priority: int = 1


class RouteReq(BaseModel):
    project_id: int
    name: str
    description: str = ""
    policy: str = "lb"
    cache_service_id: int | None = None
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
    policy = _check_policy(req.policy)
    cache_service_id = _check_cache(db, req.cache_service_id)
    r = ModelRoute(project_id=req.project_id, name=name,
                   description=req.description or "", policy=policy,
                   cache_service_id=cache_service_id, created_by=user.id)
    db.add(r)
    db.flush()
    _apply_targets(db, r, [t.model_dump() for t in req.targets], user, policy)
    _mark_pending(r)
    db.commit()
    db.refresh(r)
    return _route_brief(r, db)


@router.get("/api/inference/routes/{rid}")
def get_route(rid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _route_brief(_get_route(db, rid, user), db)


def _apply_route_update(db, r: ModelRoute, req: dict, user) -> bool:
    """路由字段应用（创建/编辑共用）；返回缓存关联是否变化（供重启目标判定）。"""
    cache_changed = False
    old_cache = r.cache_service_id
    if "name" in req and req["name"] != r.name:
        r.name = _validate_route_name(db, req["name"], exclude_id=r.id)
    if "description" in req:
        r.description = req["description"]
    if "enabled" in req:
        r.enabled = bool(req["enabled"])
    if "policy" in req:
        r.policy = _check_policy(req["policy"])
    if "cache_service_id" in req:
        r.cache_service_id = _check_cache(db, req["cache_service_id"])
    if "targets" in req:
        _apply_targets(db, r, req["targets"], user, r.policy)
    if "cache_service_id" in req and r.cache_service_id != old_cache:
        cache_changed = True
    return cache_changed


@router.put("/api/inference/routes/{rid}")
def update_route(rid: int, req: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """编辑：name/description/enabled/policy/cache_service_id/targets（目标全量替换）。"""
    r = _get_route(db, rid, user)
    req = {k: v for k, v in (req or {}).items() if v is not None or k in ("enabled", "cache_service_id")}
    if not req:
        raise HTTPException(400, "无更新字段")
    cache_changed = _apply_route_update(db, r, req, user)
    _mark_pending(r)
    db.commit()
    db.refresh(r)
    if cache_changed:
        _restart_targets(db, r)  # 路由级缓存变更 → 目标实例重新注入 env
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

def _renumber_priority(db, route_id: int, policy: str) -> None:
    """priority 策略：按 id 序重排 priority=1..n（增删目标后保持连续）。"""
    if (policy or "lb") != "priority":
        return
    ts = db.query(ModelRouteTarget).filter_by(route_id=route_id) \
        .order_by(ModelRouteTarget.id).all()
    for i, t in enumerate(ts):
        t.priority = i + 1
        t.is_fallback = i > 0
        t.weight = 100


@router.post("/api/inference/routes/{rid}/targets")
def add_target(rid: int, req: TargetIn, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    r = _get_route(db, rid, user)
    dep = _get_dep(db, req.deployment_id, user)
    exists = db.query(ModelRouteTarget).filter_by(route_id=r.id,
                                                  deployment_id=dep.id).first()
    if exists:
        raise HTTPException(400, f"部署 {dep.name} 已在此路由中（可先修改权重/容灾标记）")
    if (r.policy or "lb") == "priority":
        nxt = (db.query(func.max(ModelRouteTarget.priority))
               .filter_by(route_id=r.id).scalar() or 0) + 1
        t = ModelRouteTarget(route_id=r.id, deployment_id=dep.id,
                             weight=100, is_fallback=nxt > 1, priority=nxt)
    else:
        t = ModelRouteTarget(route_id=r.id, deployment_id=dep.id,
                             weight=_check_weight(req.weight),
                             is_fallback=bool(req.is_fallback), priority=1)
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
    if (r.policy or "lb") == "priority":
        # 优先级策略：权重/容灾/顺序均由全量目标列表管理（编辑抽屉），
        # 单条端点不接受字段变更
        if req:
            raise HTTPException(400, "优先级路由的目标权重/顺序请在路由编辑中整列调整")
        return _route_brief(r, db)
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
    _renumber_priority(db, r.id, r.policy)
    _mark_pending(r)
    db.commit()
    return _route_brief(r, db)

"""模型资产仓库：基座模型纳管（HF 下载）+ 微调产物归档 + 血缘追踪

存储位置（单副本语义）：
- 未选择：仅本地 {data_dir}/models/hub；path 型后端：挂载点下 models/hub；
- object 型后端：唯一权威副本在桶内，本地模型仓库路径是**服务缓存**
  （上传/HF 下载全量入桶后删本地；训练/推理启动前经 model_service.
  ensure_model_local 自动拉回，拉取失败任务直接报错）。
"""
import os
import logging
import shutil

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..security import get_current_user, can_access_project, require_role
from ..models import ModelAsset, Project, TrainingJob, DatasetVersion, User, StorageBackend, Deployment
from ..services import storage

log = logging.getLogger("models")
router = APIRouter(prefix="/api/models", tags=["models"])


def _check_project(db, pid: int, user: User):
    p = db.get(Project, pid)
    if not p or not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")
    return p


def _resolve_backend(db, storage_backend_id: int | None):
    """存储位置校验（写路径）：None → 默认本地；0 → 内置 rustfs（须启用）；
    >0 必须存在且（path 型）挂载点在 backend 容器内可见（FUSE 子挂载需同路径 bind，
    内置 /data/juicefs 已预配）。返回后端行；内置/未选择返回 None（object 判定用
    storage.location）。"""
    if storage_backend_id is None:
        return None
    if storage_backend_id == storage.BUILTIN_ID:
        if not storage.enabled():
            raise HTTPException(400, "平台内置对象存储未启用（未配置 S3_ENDPOINT）")
        return None
    b = db.get(StorageBackend, storage_backend_id)
    if not b:
        raise HTTPException(400, f"存储后端 #{storage_backend_id} 不存在")
    if b.type in storage.PATH_BASED_TYPES and not os.path.isdir(storage.backend_root(b)):
        raise HTTPException(400, f"存储后端 {b.name} 的挂载点 {storage.backend_root(b)} "
                                 "在平台后端容器内不可见（自定义挂载点需同路径 bind 进 backend 容器后重启）")
    return b


def _model_hub_root(db: Session, storage_backend_id: int | None) -> str:
    """模型仓库根目录：path 型后端 → <挂载点>/models/hub（与默认布局对齐）；
    object 型/未选择 → 默认 {data_dir}/models/hub（object 型该路径是服务缓存，
    权威副本在桶内，key 前缀与该路径相对 data_dir 一一对应）"""
    b = _resolve_backend(db, storage_backend_id)
    if b is not None and b.type in storage.PATH_BASED_TYPES:
        root = storage.backend_root(b)
        if root:
            return os.path.join(root, "models", "hub")
    return os.path.join(settings.data_dir, "models", "hub")


def _deploy_status_map(db: Session, model_ids: list[int]) -> dict:
    """按模型聚合部署运行状态：{model_id: {"status", "running"}}。
    status 优先级 running > creating > failed > stopped；无任何部署=none；
    running=运行中副本总数（status=running 的部署 replicas 之和）。一次批量查询，
    供列表页避免 N+1。"""
    ids = [i for i in (model_ids or []) if i]
    out = {mid: {"status": "none", "running": 0} for mid in ids}
    if not ids:
        return out
    deps = db.query(Deployment).filter(Deployment.model_asset_id.in_(ids)).all()
    prio = {"running": 4, "creating": 3, "failed": 2, "stopped": 1}
    by: dict[int, list] = {}
    for d in deps:
        by.setdefault(d.model_asset_id, []).append(d)
    for mid, ds in by.items():
        out[mid] = {
            "status": max((d.status for d in ds), key=lambda s: prio.get(s, 0)),
            "running": sum(d.replicas or 0 for d in ds if d.status == "running"),
        }
    return out


def _brief(a: ModelAsset, db: Session, _deploy: dict | None = None) -> dict:
    base = db.get(ModelAsset, a.parent_asset_id) if a.parent_asset_id else None
    _, _, label = storage.location(a.storage_backend_id, db)
    ds = _deploy if _deploy is not None else _deploy_status_map(db, [a.id])[a.id]
    return {
        "id": a.id, "project_id": a.project_id, "name": a.name, "type": a.type,
        "category": a.category or "", "tags": a.tags or [],
        "path": a.path, "source": a.source, "hf_id": a.hf_id,
        "size_bytes": a.size_bytes, "status": a.status,
        # 运行状态（聚合该模型全部部署，区别于上面的下载/就绪 status）：
        # none=未部署 / running=运行中 / creating=创建中 / failed=失败 / stopped=已停止
        "deploy_status": ds["status"], "deploy_running": ds["running"],
        "parent_name": base.name if base else "",
        "storage_backend_id": a.storage_backend_id,
        "storage_backend": label,
        "job_id": a.job_id, "dataset_version_ids": a.dataset_version_ids or [],
        "note": a.note, "created_at": str(a.created_at),
    }


def _size(path: str) -> int:
    if os.path.isfile(path):  # 单文件路径（os.walk 对文件返回 0）
        try:
            return os.path.getsize(path)
        except OSError:
            return 0
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


@router.get("")
def list_models(project_id: int | None = None, mtype: str = "", category: str | None = None,
                q: str = "", db=Depends(get_db), user=Depends(get_current_user)):
    kw = q.strip()
    if project_id:
        _check_project(db, project_id, user)
        q = db.query(ModelAsset).filter(ModelAsset.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = db.query(ModelAsset).filter(ModelAsset.project_id.in_(pids or [0]))
    if mtype:
        q = q.filter(ModelAsset.type == mtype)
    # category: None=全部（未传） / ""=仅未分类 / "__all__"=全部 / 其他=精确匹配
    if category is not None and category != "__all__":
        if category == "":
            q = q.filter((ModelAsset.category.is_(None)) | (ModelAsset.category == ""))
        else:
            q = q.filter(ModelAsset.category == category)
    if kw:
        q = q.filter(ModelAsset.name.ilike(f"%{kw}%"))
    rows = q.order_by(ModelAsset.id.desc()).all()
    depmap = _deploy_status_map(db, [a.id for a in rows])  # 一次批量查部署，避免 N+1
    out = []
    for a in rows:
        b = _brief(a, db, _deploy=depmap.get(a.id))
        if a.status == "pending_download" and a.path:
            # 断点续传进度：实时统计已落地字节数（huggingface_hub 直接写 local_dir，部分文件也在内）
            b["size_bytes"] = _size(a.path)
        out.append(b)
    return out


class BaseReq(BaseModel):
    project_id: int
    name: str
    hf_id: str  # 如 Qwen/Qwen2.5-0.5B-Instruct
    note: str = ""
    category: str = ""
    tags: list[str] = []
    storage_backend_id: int | None = None  # 存储位置（注册后端）；空=平台默认


@router.post("/base")
def register_base(req: BaseReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check_project(db, req.project_id, user)
    _resolve_backend(db, req.storage_backend_id)
    if db.query(ModelAsset).filter_by(project_id=req.project_id, name=req.name).first():
        raise HTTPException(400, "同名模型已存在")
    path = os.path.join(_model_hub_root(db, req.storage_backend_id), req.name)
    a = ModelAsset(project_id=req.project_id, name=req.name, type="base", path=path,
                   source=f"hf:{req.hf_id}", hf_id=req.hf_id,
                   category=req.category or "", tags=req.tags or [],
                   storage_backend_id=req.storage_backend_id,
                   status="pending_download" if not os.path.exists(path) else "ready",
                   note=req.note, created_by=user.id)
    if os.path.exists(path):
        a.size_bytes = _size(path)
        a.status = "ready"
    db.add(a)
    db.commit()
    db.refresh(a)
    return _brief(a, db)


@router.get("/search")
def search_models(source: str = "hf", q: str = "", limit: int = 20,
                  db=Depends(get_db), user=Depends(get_current_user)):
    """模型搜索（后端代理 HuggingFace / ModelScope，前端不直连，规避网络与 CORS 问题）"""
    if not q.strip():
        return []
    limit = max(1, min(int(limit), 50))
    try:
        import httpx
        if source == "ms":
            # ModelScope OpenAPI（/openapi/v1 前缀，search 自由文本检索），
            # 响应形如 {"data":{"models":[{"id","display_name","downloads","likes",...}]}}
            r = httpx.get("https://modelscope.cn/openapi/v1/models",
                          params={"search": q.strip(), "sort": "downloads",
                                  "page_number": 1, "page_size": limit},
                          timeout=15, follow_redirects=True)
            r.raise_for_status()
            data = ((r.json().get("data") or {}).get("models")) or []
            return [{
                "source": "ms",
                "id": m.get("id") or "",
                "name": (m.get("id") or "").split("/")[-1],
                "display": m.get("display_name") or m.get("id") or "",
                "downloads": m.get("downloads") or 0,
                "likes": m.get("likes") or 0,
            } for m in data[:limit]]
        base = (settings.hf_endpoint or "https://hf-mirror.com").rstrip("/")
        r = httpx.get(f"{base}/api/models",
                      params={"search": q.strip(), "limit": limit,
                              "sort": "downloads", "direction": -1},
                      timeout=15, follow_redirects=True)
        r.raise_for_status()
        return [{
            "source": "hf",
            "id": m.get("id") or "",
            "name": m.get("modelId") or m.get("id") or "",
            "display": m.get("id") or "",
            "downloads": m.get("downloads") or 0,
            "likes": m.get("likes") or 0,
        } for m in r.json()[:limit]]
    except Exception as e:
        raise HTTPException(502, f"搜索源暂不可用（{source}）: {str(e)[:200]}")


@router.post("/upload")
async def upload_model(project_id: int = Form(...), name: str = Form(...),
                       note: str = Form(""), category: str = Form(""),
                       tags: str = Form(""),
                       storage_backend_id: int | None = Form(None),
                       file: UploadFile = File(...),
                       db=Depends(get_db), user=Depends(get_current_user)):
    """本地上传：zip 包（含 config.json / *.safetensors 的完整模型目录）→ 解压入仓

    storage_backend_id 缺省→仅本地；0=内置 rustfs 主桶；>0 且 path 型后端时解压到
    该挂载点下（唯一副本）；object 型（含内置）：先上传到该桶，全量成功后删除本地
    副本（桶内为唯一权威副本，本地模型仓库路径变为服务缓存，训练/推理启动前自动拉回）。
    """
    import io
    import re
    import zipfile
    _check_project(db, project_id, user)
    sbid = int(storage_backend_id) if storage_backend_id is not None else None
    b = _resolve_backend(db, sbid)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$", name or ""):
        raise HTTPException(400, "模型名仅允许字母数字 . _ -（1-80 位）")
    if db.query(ModelAsset).filter_by(project_id=project_id, name=name).first():
        raise HTTPException(400, "同名模型已存在")
    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(400, "需上传 zip 包（将模型目录压缩为 .zip 后上传）")
    dest = os.path.realpath(os.path.join(_model_hub_root(db, sbid), name))
    if os.path.exists(dest):
        raise HTTPException(400, "目标目录已存在")
    os.makedirs(dest, exist_ok=True)

    def _abort():
        shutil.rmtree(dest, ignore_errors=True)

    try:
        for info in zf.infolist():
            target = os.path.realpath(os.path.join(dest, info.filename))
            if target != dest and not target.startswith(dest + os.sep):
                raise HTTPException(400, "zip 包含非法路径（zip-slip），已拒绝")
        zf.extractall(dest)
    except HTTPException:
        _abort()
        raise
    except Exception as e:
        _abort()
        raise HTTPException(400, f"解压失败: {str(e)[:200]}")
    files = os.listdir(dest)
    if not (os.path.exists(os.path.join(dest, "config.json"))
            or any(f.endswith(".safetensors") for f in files)):
        _abort()
        raise HTTPException(400, "zip 不是完整模型目录（缺 config.json 或 *.safetensors）")
    size = _size(dest)
    # object 型单副本（含内置 rustfs）：全量上传该桶后删除本地副本（不完整则保留）
    kind, cfg, _ = storage.location(sbid, db)
    if kind == "object":
        n_files = sum(len(fs) for _, _, fs in os.walk(dest))
        ok_n = storage.put_dir(dest, cfg)
        if n_files and ok_n >= n_files:
            shutil.rmtree(dest, ignore_errors=True)  # 桶内为唯一权威副本
        elif ok_n:
            log.warning("模型 %s 桶同步不完整（%d/%d），保留本地副本", name, ok_n, n_files)
    a = ModelAsset(project_id=project_id, name=name, type="base", path=dest,
                   source="upload", status="ready", size_bytes=size,
                   category=category or "",
                   tags=[t.strip() for t in tags.split(",") if t.strip()],
                   storage_backend_id=sbid,
                   note=note or f"local upload: {file.filename}", created_by=user.id)
    db.add(a)
    db.commit()
    db.refresh(a)
    return _brief(a, db)


class ArtifactReq(BaseModel):
    project_id: int
    name: str
    type: str  # lora/merged
    path: str
    base_model_name: str = ""
    note: str = ""
    category: str = ""
    tags: list[str] = []
    storage_backend_id: int | None = None  # 存储位置（path 型后端产物须在该挂载点下）


@router.post("")
def register_artifact(req: ArtifactReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check_project(db, req.project_id, user)
    if req.type not in ("lora", "merged"):
        raise HTTPException(400, "type 需为 lora/merged")
    b = _resolve_backend(db, req.storage_backend_id)
    if not os.path.exists(req.path):
        raise HTTPException(400, f"路径不存在: {req.path}")
    if b is not None and b.type in storage.PATH_BASED_TYPES:
        root = os.path.realpath(storage.backend_root(b))
        if not os.path.realpath(req.path).startswith(root + os.sep):
            raise HTTPException(400, f"产物路径不在存储后端 {b.name} 的挂载点（{root}）内")
    if db.query(ModelAsset).filter_by(project_id=req.project_id, name=req.name).first():
        raise HTTPException(400, "同名模型已存在")
    parent = None
    if req.base_model_name:
        parent = db.query(ModelAsset).filter_by(project_id=req.project_id, name=req.base_model_name).first()
    a = ModelAsset(project_id=req.project_id, name=req.name, type=req.type, path=req.path,
                   source=req.base_model_name, parent_asset_id=parent.id if parent else None,
                   category=req.category or "", tags=req.tags or [],
                   storage_backend_id=req.storage_backend_id,
                   note=req.note, created_by=user.id)
    a.size_bytes = _size(req.path)
    db.add(a)
    db.commit()
    db.refresh(a)
    return _brief(a, db)


class MetaReq(BaseModel):
    category: str | None = None
    tags: list[str] | None = None
    note: str | None = None


@router.put("/{aid}/meta")
def update_meta(aid: int, req: MetaReq, db=Depends(get_db), user=Depends(get_current_user)):
    """修改分类/标签/备注（不碰权重文件）"""
    a = db.get(ModelAsset, aid)
    if not a:
        raise HTTPException(404, "模型不存在")
    _check_project(db, a.project_id, user)
    if req.category is not None:
        a.category = req.category
    if req.tags is not None:
        a.tags = [t.strip() for t in req.tags if t.strip()]
    if req.note is not None:
        a.note = req.note
    db.commit()
    db.refresh(a)
    return _brief(a, db)


@router.get("/{aid}")
def get_model(aid: int, db=Depends(get_db), user=Depends(get_current_user)):
    a = db.get(ModelAsset, aid)
    if not a:
        raise HTTPException(404, "模型不存在")
    _check_project(db, a.project_id, user)
    return _brief(a, db)


@router.get("/{aid}/lineage")
def lineage(aid: int, db=Depends(get_db), user=Depends(get_current_user)):
    """血缘：父模型链 + 训练任务 + 数据集版本 + 由它产出的子模型"""
    a = db.get(ModelAsset, aid)
    if not a:
        raise HTTPException(404, "模型不存在")
    _check_project(db, a.project_id, user)
    chain = []
    cur = a
    seen = set()
    while cur and cur.id not in seen:
        seen.add(cur.id)
        chain.append({"id": cur.id, "name": cur.name, "type": cur.type, "status": cur.status})
        cur = db.get(ModelAsset, cur.parent_asset_id) if cur.parent_asset_id else None
    jobs = db.query(TrainingJob).filter(TrainingJob.id.in_(
        [j.id for j in db.query(TrainingJob).filter(TrainingJob.base_model_id == aid).all()])).all() if a else []
    # 该模型作为 base 派生出的模型
    children = db.query(ModelAsset).filter(ModelAsset.parent_asset_id == aid).all()
    # 训练任务（以其为 base 的）
    train_jobs = db.query(TrainingJob).filter(TrainingJob.base_model_id == aid).limit(20).all()
    dsvs = []
    for vid in (a.dataset_version_ids or []):
        v = db.get(DatasetVersion, vid)
        if v:
            dsvs.append({"id": v.id, "version": v.version, "note": v.note})
    return {
        "asset": _brief(a, db),
        "chain": chain,
        "children": [_brief(c, db) for c in children],
        "training_jobs": [{"id": j.id, "name": j.name, "status": j.status,
                           "stage": j.stage, "created_at": str(j.created_at)} for j in train_jobs],
        "dataset_versions": dsvs,
    }


@router.delete("/{aid}")
def delete_model(aid: int, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin", "developer"))):
    a = db.get(ModelAsset, aid)
    if not a:
        raise HTTPException(404, "模型不存在")
    _check_project(db, a.project_id, user)
    # 引用检查：运行中部署 / 训练任务 / 评测 / 子模型
    from ..models import Deployment, TrainingJob, EvalTask
    running_dep = (db.query(Deployment)
                   .filter(Deployment.model_asset_id == aid, Deployment.status == "running").first())
    if running_dep:
        raise HTTPException(400, f"模型正被运行中部署「{running_dep.name}」使用，不可删除")
    job = db.query(TrainingJob).filter(TrainingJob.base_model_id == aid).first() or \
        db.query(TrainingJob).filter(TrainingJob.adapter_asset_id == aid).first()
    if job and job.status in ("queued", "running"):
        raise HTTPException(400, f"模型正被训练任务 #{job.id}（{job.status}）使用，不可删除")
    if db.query(EvalTask).filter(EvalTask.target_asset_id == aid,
                                 EvalTask.status.in_(["pending", "running"])).first():
        raise HTTPException(400, "模型被运行中的评测任务引用，不可删除")
    children = db.query(ModelAsset).filter(ModelAsset.parent_asset_id == aid).count()
    if children:
        raise HTTPException(400, f"模型有 {children} 个子模型（血缘依赖），请先删除子模型")
    path = a.path
    bid = a.storage_backend_id
    b = db.get(StorageBackend, bid) if bid and bid != storage.BUILTIN_ID else None
    db.delete(a)
    db.commit()
    # 清理：object 型桶内唯一副本（含内置 rustfs；force：本地缓存可能已删）
    #      + 本地目录（限平台数据目录或 path 型后端挂载点内）
    import shutil
    try:
        kind, cfg, _ = storage.location(bid, db)
        if kind == "object":
            storage.delete_dir(path, cfg, force=True)
        allowed_roots = [os.path.realpath(settings.data_dir)]
        if b is not None and b.type in storage.PATH_BASED_TYPES:
            allowed_roots.append(os.path.realpath(storage.backend_root(b)))
        rp = os.path.realpath(path)
        if any(rp.startswith(r + os.sep) for r in allowed_roots) and os.path.isdir(rp):
            shutil.rmtree(rp, ignore_errors=True)
    except Exception:
        pass
    return {"ok": True}


class BatchDeleteReq(BaseModel):
    ids: list[int]


@router.post("/batch-delete")
def batch_delete_models(req: BatchDeleteReq, db=Depends(get_db),
                        user=Depends(require_role("super_admin", "tenant_admin", "developer"))):
    """批量删除：逐个执行单删逻辑（含引用检查），返回部分结果"""
    deleted: list[int] = []
    failed: list[dict] = []
    for aid in req.ids:
        try:
            delete_model(aid, db=db, user=user)
            deleted.append(aid)
        except HTTPException as e:
            db.rollback()
            failed.append({"id": aid, "error": str(e.detail)})
    return {"deleted": deleted, "failed": failed}

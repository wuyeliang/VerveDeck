"""模型资产仓库：基座模型纳管（HF 下载）+ 微调产物归档 + 血缘追踪"""
import os
import shutil

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..security import get_current_user, can_access_project, require_role
from ..models import ModelAsset, Project, TrainingJob, DatasetVersion, User

router = APIRouter(prefix="/api/models", tags=["models"])


def _check_project(db, pid: int, user: User):
    p = db.get(Project, pid)
    if not p or not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")
    return p


def _brief(a: ModelAsset, db: Session) -> dict:
    base = db.get(ModelAsset, a.parent_asset_id) if a.parent_asset_id else None
    return {
        "id": a.id, "project_id": a.project_id, "name": a.name, "type": a.type,
        "path": a.path, "source": a.source, "hf_id": a.hf_id,
        "size_bytes": a.size_bytes, "status": a.status,
        "parent_name": base.name if base else "",
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
def list_models(project_id: int | None = None, mtype: str = "", db=Depends(get_db),
                user=Depends(get_current_user)):
    if project_id:
        _check_project(db, project_id, user)
        q = db.query(ModelAsset).filter(ModelAsset.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = db.query(ModelAsset).filter(ModelAsset.project_id.in_(pids or [0]))
    if mtype:
        q = q.filter(ModelAsset.type == mtype)
    rows = q.order_by(ModelAsset.id.desc()).all()
    out = []
    for a in rows:
        b = _brief(a, db)
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


@router.post("/base")
def register_base(req: BaseReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check_project(db, req.project_id, user)
    if db.query(ModelAsset).filter_by(project_id=req.project_id, name=req.name).first():
        raise HTTPException(400, "同名模型已存在")
    path = os.path.join(settings.data_dir, "models", "hub", req.name)
    a = ModelAsset(project_id=req.project_id, name=req.name, type="base", path=path,
                   source=f"hf:{req.hf_id}", hf_id=req.hf_id,
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
                       note: str = Form(""),
                       file: UploadFile = File(...),
                       db=Depends(get_db), user=Depends(get_current_user)):
    """本地上传：zip 包（含 config.json / *.safetensors 的完整模型目录）→ 解压入仓"""
    import io
    import re
    import zipfile
    _check_project(db, project_id, user)
    if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,79}$", name or ""):
        raise HTTPException(400, "模型名仅允许字母数字 . _ -（1-80 位）")
    if db.query(ModelAsset).filter_by(project_id=project_id, name=name).first():
        raise HTTPException(400, "同名模型已存在")
    content = await file.read()
    try:
        zf = zipfile.ZipFile(io.BytesIO(content))
    except zipfile.BadZipFile:
        raise HTTPException(400, "需上传 zip 包（将模型目录压缩为 .zip 后上传）")
    dest = os.path.realpath(os.path.join(settings.data_dir, "models", "hub", name))
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
    a = ModelAsset(project_id=project_id, name=name, type="base", path=dest,
                   source="upload", status="ready", size_bytes=_size(dest),
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


@router.post("")
def register_artifact(req: ArtifactReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check_project(db, req.project_id, user)
    if req.type not in ("lora", "merged"):
        raise HTTPException(400, "type 需为 lora/merged")
    if not os.path.exists(req.path):
        raise HTTPException(400, f"路径不存在: {req.path}")
    if db.query(ModelAsset).filter_by(project_id=req.project_id, name=req.name).first():
        raise HTTPException(400, "同名模型已存在")
    parent = None
    if req.base_model_name:
        parent = db.query(ModelAsset).filter_by(project_id=req.project_id, name=req.base_model_name).first()
    a = ModelAsset(project_id=req.project_id, name=req.name, type=req.type, path=req.path,
                   source=req.base_model_name, parent_asset_id=parent.id if parent else None,
                   note=req.note, created_by=user.id)
    a.size_bytes = _size(req.path)
    db.add(a)
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
    db.delete(a)
    db.commit()
    # 磁盘清理：仅限平台数据目录内的路径
    import os, shutil
    try:
        data_root = os.path.realpath(settings.data_dir)
        rp = os.path.realpath(path)
        if rp.startswith(data_root + os.sep) and os.path.isdir(rp):
            shutil.rmtree(rp, ignore_errors=True)
    except Exception:
        pass
    return {"ok": True}

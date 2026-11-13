"""数据工程：多源解析 / 清洗脱敏 / 合成数据(QA/CoT) / 协同标注(Argilla) / DVC 版本控制"""
import json
import os
import time
import threading
import logging

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db, SessionLocal
from ..security import get_current_user, can_access_project, require_role
from ..models import (Dataset, DatasetVersion, DataTask, Project, User, Deployment,
                      ModelAsset, GuardRule)
from ..services import dataset_service as ds
from ..services import dvc_service

log = logging.getLogger("datasets")
router = APIRouter(prefix="/api/datasets", tags=["datasets"])


def _proj(db, pid: int, user: User) -> Project:
    p = db.get(Project, pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    if not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")
    return p


def _get_ds(db, dsid: int, user: User) -> Dataset:
    d = db.get(Dataset, dsid)
    if not d:
        raise HTTPException(404, "数据集不存在")
    _proj(db, d.project_id, user)
    return d


def _next_version(db, dsid: int) -> int:
    row = db.query(DatasetVersion).filter_by(dataset_id=dsid).count()
    return row + 1


def _create_version(db, dset: Dataset, rows: list[dict], uid: int, fmt: str,
                    note: str, raw_md: str = "") -> DatasetVersion:
    # uid 为纯 int：后台线程里 ORM user 对象已过期（expire_on_commit），只能传值
    repo = ds.dataset_repo_dir(dset.project_id, dset.name)
    ver_no = _next_version(db, dset.id)
    vdir = ds.version_dir(repo, ver_no)
    os.makedirs(vdir, exist_ok=True)
    n = ds.write_jsonl(ds.version_data_file(repo, ver_no), rows)
    if raw_md:
        with open(os.path.join(vdir, "raw.md"), "w", encoding="utf-8") as f:
            f.write(raw_md)
    ver = DatasetVersion(dataset_id=dset.id, version=ver_no, size_bytes=os.path.getsize(
        ds.version_data_file(repo, ver_no)) if os.path.exists(ds.version_data_file(repo, ver_no)) else 0,
        sample_count=n, fmt=fmt, status="draft", note=note, created_by=uid)
    db.add(ver)
    db.commit()
    # 主存储同步到 rustfs（本地缓存已写入，S3 未配置/失败时静默降级）
    from ..services import storage
    storage.put_dir(vdir)
    db.refresh(ver)
    return ver


# ---------- CRUD ----------

class DsReq(BaseModel):
    project_id: int
    name: str
    description: str = ""


@router.get("")
def list_datasets(project_id: int | None = None, db=Depends(get_db), user=Depends(get_current_user)):
    q = db.query(Dataset)
    if project_id:
        _proj(db, project_id, user)
        q = q.filter(Dataset.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = q.filter(Dataset.project_id.in_(pids or [0]))
    out = []
    for d in q.all():
        n = db.query(DatasetVersion).filter_by(dataset_id=d.id).count()
        out.append({"id": d.id, "project_id": d.project_id, "name": d.name,
                    "description": d.description, "versions": n,
                    "created_at": str(d.created_at)})
    return out


@router.post("")
def create_dataset(req: DsReq, db=Depends(get_db), user=Depends(get_current_user)):
    _proj(db, req.project_id, user)
    if db.query(Dataset).filter_by(project_id=req.project_id, name=req.name).first():
        raise HTTPException(400, "数据集已存在")
    d = Dataset(project_id=req.project_id, name=req.name, description=req.description, created_by=user.id)
    db.add(d)
    db.commit()
    db.refresh(d)
    dvc_service.init_repo(ds.dataset_repo_dir(req.project_id, req.name))
    return d


@router.delete("/{dsid}")
def delete_dataset(dsid: int, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin", "developer"))):
    d = _get_ds(db, dsid, user)
    repo = ds.dataset_repo_dir(d.project_id, d.name)  # commit 后 ORM 属性过期，先捕获路径
    db.query(DatasetVersion).filter_by(dataset_id=dsid).delete()
    db.delete(d)
    db.commit()
    # 对象存储同步清理（主存储为 rustfs 时）
    from ..services import storage
    storage.delete_dir(repo)
    return {"ok": True}


# 注意：/tasks 必须在 /{dsid} 之前注册，否则被动态路由遮蔽（Starlette 按注册顺序匹配）
@router.get("/tasks")
def list_tasks(dataset_id: int | None = None, db=Depends(get_db), user=Depends(get_current_user)):
    q = db.query(DataTask)
    if dataset_id:
        _get_ds(db, dataset_id, user)
        q = q.filter(DataTask.dataset_id == dataset_id)
    rows = q.order_by(DataTask.id.desc()).limit(100).all()
    return [{"id": t.id, "kind": t.kind, "dataset_id": t.dataset_id,
             "source_version_id": t.source_version_id, "output_version_id": t.output_version_id,
             "file_name": t.file_name, "status": t.status, "progress": t.progress,
             "params": t.params, "result": t.result, "error": t.error,
             "created_at": str(t.created_at), "updated_at": str(t.updated_at)} for t in rows]


@router.get("/{dsid}")
def get_dataset(dsid: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    vers = db.query(DatasetVersion).filter_by(dataset_id=dsid).order_by(DatasetVersion.version.desc()).all()
    return {**{
        "id": d.id, "project_id": d.project_id, "name": d.name, "description": d.description,
        "created_at": str(d.created_at)},
        "versions": [_ver_brief(v) for v in vers]}


def _ver_brief(v: DatasetVersion):
    return {"id": v.id, "dataset_id": v.dataset_id, "version": v.version,
            "dvc_rev": v.dvc_rev, "size_bytes": v.size_bytes, "sample_count": v.sample_count,
            "fmt": v.fmt, "status": v.status, "note": v.note,
            "created_at": str(v.created_at), "created_by": v.created_by}


@router.get("/{dsid}/versions")
def list_versions(dsid: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    vers = db.query(DatasetVersion).filter_by(dataset_id=dsid).order_by(DatasetVersion.version.desc()).all()
    return [_ver_brief(v) for v in vers]


@router.post("/{dsid}/versions/{vid}/commit")
def commit_version(dsid: int, vid: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    v = db.get(DatasetVersion, vid)
    if not v or v.dataset_id != dsid:
        raise HTTPException(404, "版本不存在")
    repo = ds.dataset_repo_dir(d.project_id, d.name)
    rev = dvc_service.add_and_commit(repo, f"v{v.version}: {v.note[:80]}")
    v.dvc_rev = rev
    v.status = "committed"
    db.commit()
    return {"ok": True, "dvc_rev": rev}


@router.get("/{dsid}/versions/{vid}/samples")
def preview(dsid: int, vid: int, page: int = 1, size: int = 20, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    v = db.get(DatasetVersion, vid)
    if not v or v.dataset_id != dsid:
        raise HTTPException(404, "版本不存在")
    rows = ds.read_jsonl(ds.ensure_version_local(ds.dataset_repo_dir(d.project_id, d.name), v.version))
    total = len(rows)
    start = (page - 1) * size
    return {"total": total, "page": page, "size": size, "items": rows[start:start + size]}


@router.get("/{dsid}/versions/{vid}/raw")
def raw_file(dsid: int, vid: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    v = db.get(DatasetVersion, vid)
    if not v or v.dataset_id != dsid:
        raise HTTPException(404, "版本不存在")
    from fastapi.responses import PlainTextResponse
    p = ds.ensure_version_raw(ds.dataset_repo_dir(d.project_id, d.name), v.version)
    if not os.path.exists(p):
        raise HTTPException(404, "无原始文档")
    with open(p, encoding="utf-8", errors="ignore") as f:
        return PlainTextResponse(f.read())


@router.get("/{dsid}/dvc-log")
def dvc_log(dsid: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    return dvc_service.log(ds.dataset_repo_dir(d.project_id, d.name), 30)


# ---------- 上传生成版本 ----------

class UploadVersionReq(BaseModel):
    fmt: str = "alpaca"  # alpaca/dpo
    note: str = ""


@router.post("/{dsid}/versions/upload")
async def upload_version(dsid: int, fmt: str = "alpaca", note: str = "",
                         file: UploadFile = File(...), db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    content = await file.read()
    text = content.decode("utf-8", "ignore")
    rows = []
    fname = (file.filename or "").lower()
    if fname.endswith((".json",)) or text.strip().startswith("["):
        try:
            data = json.loads(text)
            rows = data if isinstance(data, list) else data.get("data", [])
        except json.JSONDecodeError:
            raise HTTPException(400, "JSON 解析失败")
    else:  # jsonl / csv 按 jsonl 处理
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    normed = [r for r in (ds.normalize(r, fmt) for r in rows) if r]
    if not normed:
        raise HTTPException(400, "未解析出有效样本（需 instruction/output 或 prompt/chosen/rejected 字段）")
    ver = _create_version(db, d, normed, user.id, fmt, note or f"upload:{file.filename}")
    return _ver_brief(ver)


# ---------- 文档解析（MinerU 常驻服务，不新起容器） ----------

class ParseReq(BaseModel):
    dataset_id: int
    as_sft: bool = True  # markdown → SFT 知识样例


@router.post("/parse")
async def parse_doc(dataset_id: int, as_sft: bool = True,
                    file: UploadFile = File(...), db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dataset_id, user)
    task = DataTask(kind="parse", dataset_id=d.id, file_name=file.filename or "doc",
                    status="pending", created_by=user.id,
                    params={"as_sft": as_sft})
    db.add(task)
    db.commit()
    db.refresh(task)
    updir = os.path.join(settings.data_dir, "uploads", str(d.id))
    os.makedirs(updir, exist_ok=True)
    fpath = os.path.join(updir, f"{int(time.time())}_{file.filename}")
    with open(fpath, "wb") as f:
        f.write(await file.read())
    # commit 已使 ORM 属性过期：线程闭包只能捕获纯值
    did, tid, uid, fname = d.id, task.id, user.id, (file.filename or "doc.pdf")

    def _work():
        db2 = SessionLocal()
        try:
            t = db2.get(DataTask, tid)
            t.status = "running"
            db2.commit()
            out = ds.mineru_parse(fpath, fname)
            md = out.get("markdown", "")
            dset = db2.get(Dataset, did)
            rows = ds.markdown_to_alpaca(md) if as_sft else []
            ver = _create_version(db2, dset, rows or [{"instruction": md[:2000], "input": "", "output": md[:2000]}],
                                  uid, "alpaca", f"parse:{fname}", raw_md=md)
            t.output_version_id = ver.id
            t.status = "done"
            t.result = {"pages": out.get("pages"), "samples": len(rows or [1])}
        except Exception as e:
            t2 = db2.get(DataTask, tid)
            t2.status = "failed"
            t2.error = str(e)[:4000]
        finally:
            db2.commit()
            db2.close()

    threading.Thread(target=_work, daemon=True).start()
    return {"task_id": task.id}


# ---------- 清洗脱敏 ----------

class CleanReq(BaseModel):
    dataset_id: int
    source_version_id: int
    pii: list[str] = ["phone", "idcard", "email", "bankcard"]
    custom_regex: list[dict] = []
    min_len: int = 0
    dedup: bool = True


@router.post("/clean")
def clean(req: CleanReq, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, req.dataset_id, user)
    v = db.get(DatasetVersion, req.source_version_id)
    if not v or v.dataset_id != d.id:
        raise HTTPException(404, "源版本不存在")
    task = DataTask(kind="clean", dataset_id=d.id, source_version_id=v.id,
                    status="pending", created_by=user.id, params=req.model_dump())
    db.add(task)
    db.commit()
    db.refresh(task)
    # commit 已使 ORM 属性过期：线程闭包只能捕获纯值
    did, tid, uid = d.id, task.id, user.id
    vversion, vfmt = v.version, v.fmt
    params = req.model_dump()

    def _work():
        db2 = SessionLocal()
        try:
            t = db2.get(DataTask, tid)
            t.status = "running"
            db2.commit()
            dset = db2.get(Dataset, did)
            rows = ds.read_jsonl(ds.ensure_version_local(ds.dataset_repo_dir(dset.project_id, dset.name), vversion))
            out, stats = ds.clean_rows(rows, params)
            ver = _create_version(db2, dset, out, uid, vfmt, f"clean: {json.dumps(stats, ensure_ascii=False)}")
            t.output_version_id = ver.id
            t.status = "done"
            t.result = stats
        except Exception as e:
            t2 = db2.get(DataTask, tid)
            t2.status = "failed"
            t2.error = str(e)[:4000]
        finally:
            db2.commit()
            db2.close()

    threading.Thread(target=_work, daemon=True).start()
    return {"task_id": task.id}


# ---------- 合成数据（QA / CoT） ----------

class SynthReq(BaseModel):
    dataset_id: int
    source_version_id: int
    mode: str = "qa"  # qa / cot
    n_per_chunk: int = 3
    deployment_id: int | None = None  # 用平台内部署的模型
    base_url: str | None = None       # 或外部 OpenAI 兼容端点
    api_key: str = ""
    model: str = ""


@router.post("/synth")
def synth(req: SynthReq, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, req.dataset_id, user)
    v = db.get(DatasetVersion, req.source_version_id)
    if not v or v.dataset_id != d.id:
        raise HTTPException(404, "源版本不存在")
    if not req.deployment_id and not req.base_url:
        raise HTTPException(400, "需指定 deployment_id 或 base_url")
    if req.deployment_id:
        dep = db.get(Deployment, req.deployment_id)
        if not dep or dep.status != "running":
            raise HTTPException(400, "指定部署未运行")
        base_url = "http://" + dep.endpoint + "/v1"  # endpoint 存储为 IP:port
        model = req.model or dep.name
        api_key = ""
    else:
        base_url, model, api_key = req.base_url, req.model, req.api_key

    task = DataTask(kind="synth", dataset_id=d.id, source_version_id=v.id,
                    status="pending", created_by=user.id,
                    params={"mode": req.mode, "n": req.n_per_chunk, "model": model})
    db.add(task)
    db.commit()
    db.refresh(task)

    # commit 已使 ORM 属性过期：线程闭包只能捕获纯值
    did, tid, uid = d.id, task.id, user.id
    vversion = v.version

    def _work():
        db2 = SessionLocal()
        try:
            t = db2.get(DataTask, tid)
            t.status = "running"
            db2.commit()
            dset = db2.get(Dataset, did)
            repo = ds.dataset_repo_dir(dset.project_id, dset.name)
            raw_md = ""
            rawp = ds.ensure_version_raw(repo, vversion)
            if os.path.exists(rawp):
                with open(rawp, encoding="utf-8", errors="ignore") as f:
                    raw_md = f.read()
            if not raw_md:
                rows0 = ds.read_jsonl(ds.ensure_version_local(repo, vversion))
                raw_md = "\n\n".join((r.get("instruction", "") or "")[:800] for r in rows0[:50])
            chunks = ds.chunk_markdown(raw_md)[: 200]
            if not chunks:
                raise RuntimeError("源版本无可用文本")
            state = {"i": 0}

            def _cb(i, total):
                state["i"] = i
                t2 = db2.get(DataTask, tid)
                t2.progress = int(i * 100 / total)
                db2.commit()

            rows = ds.synth_from_chunks(chunks, req.mode, req.n_per_chunk, base_url, model,
                                        api_key, _cb)
            normed = [r for r in (ds.normalize(r, "alpaca") for r in rows) if r]
            ver = _create_version(db2, dset, normed, uid, "alpaca",
                                  f"synth:{req.mode} model={model} n={req.n_per_chunk}")
            t.output_version_id = ver.id
            t.status = "done"
            t.progress = 100
            t.result = {"generated": len(normed), "chunks": len(chunks)}
        except Exception as e:
            t2 = db2.get(DataTask, tid)
            t2.status = "failed"
            t2.error = str(e)[:4000]
        finally:
            db2.commit()
            db2.close()

    threading.Thread(target=_work, daemon=True).start()
    return {"task_id": task.id}


# ---------- 协同标注（Argilla） ----------

class AnnotateReq(BaseModel):
    version_id: int


@router.post("/{dsid}/annotate")
def annotate(dsid: int, req: AnnotateReq, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    v = db.get(DatasetVersion, req.version_id)
    if not v or v.dataset_id != dsid:
        raise HTTPException(404, "版本不存在")
    rows = ds.read_jsonl(ds.version_data_file(ds.dataset_repo_dir(d.project_id, d.name), v.version))
    url = ds.push_to_argilla(f"{d.name}-v{v.version}", rows, f"p{d.project_id}")
    return {"argilla_url": url, "samples": len(rows)}

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
    # 存储位置：path 型后端仓库直接建在挂载点下；object 型/未选择走默认本地仓库
    repo = ds.dataset_repo_dir(dset.project_id, dset.name, dset=dset, db=db)
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
    # 对象存储同步到所选存储位置：未选择→主存储 rustfs（历史默认）；
    # object 型后端→该桶；path 型后端仓库已直接在挂载点上，不另存副副本
    # （如 juicefs 数据块本就在对象存储，避免双份占用）。未配置/失败静默降级。
    from ..models import StorageBackend
    from ..services import storage
    b = db.get(StorageBackend, dset.storage_backend_id) if dset.storage_backend_id else None
    if b is None:
        storage.put_dir(vdir)
    elif b.type == "object":
        storage.put_dir(vdir, b.config or {})
    db.refresh(ver)
    return ver


# ---------- CRUD ----------

class DsReq(BaseModel):
    project_id: int
    name: str
    description: str = ""
    kind: str = "train"  # train/eval
    scenario: str = "text"  # text/vlm
    storage_backend_id: int | None = None  # 存储位置（注册后端）；空=平台默认


@router.get("")
def list_datasets(project_id: int | None = None, q: str = "", db=Depends(get_db), user=Depends(get_current_user)):
    kw = q.strip()
    q = db.query(Dataset)
    if project_id:
        _proj(db, project_id, user)
        q = q.filter(Dataset.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = q.filter(Dataset.project_id.in_(pids or [0]))
    if kw:
        q = q.filter(Dataset.name.ilike(f"%{kw}%"))
    from ..models import StorageBackend
    bmap = {b.id: f"{b.name}（{b.type}）" for b in db.query(StorageBackend).all()}
    out = []
    for d in q.all():
        vers = db.query(DatasetVersion).filter_by(dataset_id=d.id).all()
        latest = max((v.version for v in vers), default=0)
        latest_status = next((v.status for v in vers if v.version == latest), "")
        out.append({"id": d.id, "project_id": d.project_id, "name": d.name,
                    "description": d.description, "versions": len(vers),
                    "kind": d.kind, "scenario": d.scenario,
                    "storage_backend_id": d.storage_backend_id,
                    "storage_backend": bmap.get(d.storage_backend_id or 0, ""),
                    "latest_version": latest, "samples": sum(v.sample_count for v in vers),
                    "published": latest_status == "committed",
                    "created_at": str(d.created_at)})
    return out


@router.post("")
def create_dataset(req: DsReq, db=Depends(get_db), user=Depends(get_current_user)):
    _proj(db, req.project_id, user)
    if req.kind not in ("train", "eval"):
        raise HTTPException(400, "kind 仅支持 train/eval")
    if req.scenario not in ("text", "vlm"):
        raise HTTPException(400, "scenario 仅支持 text/vlm")
    from ..models import StorageBackend
    from ..services import storage
    if req.storage_backend_id:
        b = db.get(StorageBackend, req.storage_backend_id)
        if not b:
            raise HTTPException(400, f"存储后端 #{req.storage_backend_id} 不存在")
        # 可见性守卫：path 型后端挂载点须在 backend 容器内可见（FUSE 子挂载需同路径 bind，
        # 内置 /data/juicefs 已在 compose 中预配），否则创建成功但写文件必失败
        if b.type in storage.PATH_BASED_TYPES and not os.path.isdir(storage.backend_root(b)):
            raise HTTPException(400, f"存储后端 {b.name} 的挂载点 {storage.backend_root(b)} "
                                     "在平台后端容器内不可见（自定义挂载点需同路径 bind 进 backend 容器后重启）")
    if db.query(Dataset).filter_by(project_id=req.project_id, name=req.name).first():
        raise HTTPException(400, "数据集已存在")
    d = Dataset(project_id=req.project_id, name=req.name, description=req.description,
                kind=req.kind, scenario=req.scenario,
                storage_backend_id=req.storage_backend_id, created_by=user.id)
    db.add(d)
    db.commit()
    db.refresh(d)
    dvc_service.init_repo(ds.dataset_repo_dir(req.project_id, req.name, dset=d, db=db))
    return d


@router.delete("/{dsid}")
def delete_dataset(dsid: int, db=Depends(get_db), user=Depends(require_role("super_admin", "tenant_admin", "developer"))):
    d = _get_ds(db, dsid, user)
    repo = ds.dataset_repo_dir(d.project_id, d.name, dset=d, db=db)  # commit 后 ORM 属性过期，先捕获路径
    bid = d.storage_backend_id  # 同上：删除提交前先捕获存储位置
    vids = {v.id for v in db.query(DatasetVersion).filter_by(dataset_id=dsid).all()}
    # 引用检查：训练任务（数据版本 / 由本数据集产物）与评测任务
    from ..models import TrainingJob, EvalTask
    for j in db.query(TrainingJob).all():
        if j.dataset_version_ids and (set(j.dataset_version_ids) & vids):
            raise HTTPException(400, f"数据集被训练任务 #{j.id}（{j.name}）引用，不可删除")
    if vids and db.query(EvalTask).filter(EvalTask.dataset_version_id.in_(list(vids))).first():
        raise HTTPException(400, "数据集版本被评测任务引用，不可删除")
    db.query(DataTask).filter_by(dataset_id=dsid).delete()
    db.query(DatasetVersion).filter_by(dataset_id=dsid).delete()
    db.delete(d)
    db.commit()
    # 清理：所选存储位置的对象存储（默认→主存储；object 型→该桶；path 型无副副本不处理）
    #      + 本地仓库目录（path 型即挂载点目录）+ 解析上传缓存
    import shutil
    from ..models import StorageBackend
    from ..services import storage
    b = db.get(StorageBackend, bid) if bid else None
    if b is None:
        storage.delete_dir(repo)
    elif b.type == "object":
        storage.delete_dir(repo, b.config or {})
    shutil.rmtree(repo, ignore_errors=True)
    shutil.rmtree(os.path.join(settings.data_dir, "uploads", str(dsid)), ignore_errors=True)
    return {"ok": True}


class BatchDeleteReq(BaseModel):
    ids: list[int]


@router.post("/batch-delete")
def batch_delete_datasets(req: BatchDeleteReq, db=Depends(get_db),
                          user=Depends(require_role("super_admin", "tenant_admin", "developer"))):
    """批量删除：逐个执行单删逻辑，返回部分结果（被引用的 id 不阻塞其余删除）"""
    deleted: list[int] = []
    failed: list[dict] = []
    for dsid in req.ids:
        try:
            delete_dataset(dsid, db=db, user=user)
            deleted.append(dsid)
        except HTTPException as e:
            db.rollback()
            failed.append({"id": dsid, "error": str(e.detail)})
    return {"deleted": deleted, "failed": failed}


# 注意：/tasks、/template 等静态路由必须在 /{dsid} 之前注册，否则被动态路由遮蔽（Starlette 按注册顺序匹配）
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


# 数据模板下载（新建流程参照：提供各格式样例 JSONL）
_TEMPLATES: dict[str, list[dict]] = {
    "alpaca": [
        {"instruction": "什么是 LoRA 微调？", "input": "", "output": "LoRA（Low-Rank Adaptation）是一种参数高效微调方法，通过训练低秩分解的权重矩阵适配器来更新模型，参数量远小于全参数微调。"},
        {"instruction": "梯度累积的作用是什么？", "input": "", "output": "梯度累积在多个小批量上累加梯度后一次性更新参数，等效于更大的 batch size，用于在显存受限时提升训练稳定性。"},
    ],
    "sharegpt": [
        {"conversations": [
            {"from": "human", "value": "你好，请介绍一下自己"},
            {"from": "gpt", "value": "你好！我是一个大语言模型助手，很高兴为你解答问题。"},
        ]},
    ],
    "dpo": [
        {"prompt": "请给出一条学习建议", "chosen": "建议使用间隔重复结合动手实践，定期回顾错题并输出总结，这样记忆更牢固。",
         "rejected": "多背几遍就行了。"},
    ],
}


@router.get("/template")
def download_template(fmt: str = "alpaca", db=Depends(get_db), user=Depends(get_current_user)):
    rows = _TEMPLATES.get(fmt)
    if not rows:
        raise HTTPException(400, f"未知格式 {fmt}（支持 alpaca/sharegpt/dpo）")
    from fastapi.responses import PlainTextResponse
    body = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n"
    return PlainTextResponse(body, media_type="application/jsonl",
                             headers={"Content-Disposition": f"attachment; filename=dataset_{fmt}_template.jsonl"})


# OSS（rustfs/S3）导入：从桶内对象拉取 JSONL/JSON 生成 v1
class ImportS3Req(BaseModel):
    dataset_id: int
    key: str  # 桶内对象 key
    fmt: str = "alpaca"
    note: str = ""


@router.post("/import-s3")
def import_s3(req: ImportS3Req, db=Depends(get_db), user=Depends(get_current_user)):
    from ..services import storage
    if not storage.enabled():
        raise HTTPException(400, "对象存储（S3/OSS）未启用")
    d = _get_ds(db, req.dataset_id, user)
    try:
        storage.ensure_bucket()
        c = storage.client()
        resp = c.get_object(settings.s3_bucket, req.key)
        data = resp.read()
        resp.close()
        resp.release_conn()
    except Exception as e:
        raise HTTPException(400, f"拉取对象 {req.key} 失败：{str(e)[:200]}")
    rows = _parse_rows(data.decode("utf-8", "ignore"), req.fmt, "s3-import")
    if not rows:
        raise HTTPException(400, "对象中未解析出有效样本（需 instruction/output 或 prompt/chosen/rejected 字段）")
    ver = _create_version(db, d, rows, user.id, req.fmt, req.note or f"s3:{req.key}")
    return _ver_brief(ver)


@router.get("/{dsid}")
def get_dataset(dsid: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    vers = db.query(DatasetVersion).filter_by(dataset_id=dsid).order_by(DatasetVersion.version.desc()).all()
    from ..models import StorageBackend
    b = db.get(StorageBackend, d.storage_backend_id) if d.storage_backend_id else None
    return {**{
        "id": d.id, "project_id": d.project_id, "name": d.name, "description": d.description,
        "kind": d.kind, "scenario": d.scenario,
        "storage_backend_id": d.storage_backend_id,
        "storage_backend": f"{b.name}（{b.type}）" if b else "",
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
    repo = ds.dataset_repo_dir(d.project_id, d.name, dset=d, db=db)
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
    repo = ds.dataset_repo_dir(d.project_id, d.name, dset=d, db=db)
    rows = ds.read_jsonl(ds.ensure_version_local(repo, v.version, dset=d, db=db))
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
    p = ds.ensure_version_raw(ds.dataset_repo_dir(d.project_id, d.name, dset=d, db=db), v.version, dset=d, db=db)
    if not os.path.exists(p):
        raise HTTPException(404, "无原始文档")
    with open(p, encoding="utf-8", errors="ignore") as f:
        return PlainTextResponse(f.read())


@router.get("/{dsid}/dvc-log")
def dvc_log(dsid: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    return dvc_service.log(ds.dataset_repo_dir(d.project_id, d.name, dset=d, db=db), 30)


# ---------- 上传生成版本 ----------

class UploadVersionReq(BaseModel):
    fmt: str = "alpaca"  # alpaca/dpo
    note: str = ""


def _parse_rows(text: str, fmt: str, fname: str) -> list[dict]:
    """JSON/JSONL 文本 -> 规范化样本行（解析失败抛 400；无有效样本返回 []）"""
    rows: list[dict] = []
    fname = (fname or "").lower()
    if fname.endswith(".json") or text.strip().startswith("["):
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
    return [r for r in (ds.normalize(r, fmt) for r in rows) if r]


@router.post("/{dsid}/versions/upload")
async def upload_version(dsid: int, fmt: str = "alpaca", note: str = "",
                         file: UploadFile = File(...), db=Depends(get_db), user=Depends(get_current_user)):
    d = _get_ds(db, dsid, user)
    content = await file.read()
    normed = _parse_rows(content.decode("utf-8", "ignore"), fmt, file.filename or "")
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
            repo = ds.dataset_repo_dir(dset.project_id, dset.name, dset=dset, db=db2)
            rows = ds.read_jsonl(ds.ensure_version_local(repo, vversion, dset=dset, db=db2))
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
            repo = ds.dataset_repo_dir(dset.project_id, dset.name, dset=dset, db=db2)
            raw_md = ""
            rawp = ds.ensure_version_raw(repo, vversion, dset=dset, db=db2)
            if os.path.exists(rawp):
                with open(rawp, encoding="utf-8", errors="ignore") as f:
                    raw_md = f.read()
            if not raw_md:
                rows0 = ds.read_jsonl(ds.ensure_version_local(repo, vversion, dset=dset, db=db2))
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

            rows, last_raw = ds.synth_from_chunks(chunks, req.mode, req.n_per_chunk, base_url,
                                                  model, api_key, _cb)
            normed = [r for r in (ds.normalize(r, "alpaca") for r in rows) if r]
            if not normed:
                # 全部 chunk 零产出时显式失败，避免任务「done 但 0 条」的静默假成功
                raise RuntimeError(
                    f"合成完成但 0 条可用样本（{len(chunks)} chunks，model={model}）："
                    f"模型输出未能解析为 JSON 问答对，建议换用能力更强的模型。"
                    f"原始输出样本: {last_raw[:300]!r}")
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
    rows = ds.read_jsonl(ds.version_data_file(
        ds.dataset_repo_dir(d.project_id, d.name, dset=d, db=db), v.version))
    url = ds.push_to_argilla(f"{d.name}-v{v.version}", rows, f"p{d.project_id}")
    return {"argilla_url": url, "samples": len(rows)}

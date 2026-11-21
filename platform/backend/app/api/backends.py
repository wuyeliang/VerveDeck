"""推理后端注册表：builtin/custom 后端 + 版本管理 + YAML 导入

builtin（vllm/sglang）：系统种子、不可删；生效镜像由镜像 pin 系统管理（镜像页），
注册表记录基线版本与参数默认值（前端表单渲染）。
custom（名须以 -custom 结尾）：版本决定镜像 + 启动命令模板（占位符渲染）；
可删（被部署引用时 409 拒绝）；YAML 导入仅创建、不覆盖。
"""
import logging
import re

import yaml
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db
from ..security import get_current_user
from ..models import Deployment, InferenceBackend, InferenceBackendVersion
from ..services import backend_registry as br
from ..services import upgrade_service

log = logging.getLogger("backends")
router = APIRouter(tags=["backends"])

_NAME_RE = re.compile(r"[a-z0-9-]+\Z")
_EDITABLE = ("description", "health_check_path", "default_env", "default_params")
_VER_EDITABLE = ("version", "image", "framework", "entrypoint", "run_command", "env", "is_default")


def _versions_of(db, bid: int):
    return (db.query(InferenceBackendVersion)
            .filter_by(backend_id=bid)
            .order_by(InferenceBackendVersion.id).all())


def _brief(b: InferenceBackend, versions: list[InferenceBackendVersion]) -> dict:
    # builtin 生效镜像 = pin 系统（运行时升级自动反映）；custom = 默认版本镜像
    if b.source == "builtin":
        try:
            current = upgrade_service.current_image(b.name)
        except Exception:
            current = versions[0].image if versions else ""
    else:
        dv = next((v for v in versions if v.is_default), None)
        current = dv.image if dv else (versions[0].image if versions else "")
    return {
        "id": b.id, "name": b.name, "source": b.source,
        "description": b.description or "",
        "health_check_path": b.health_check_path or "/v1/models",
        "default_env": b.default_env or {},
        "default_params": b.default_params or {},
        "current_image": current,
        "versions": [
            {"id": v.id, "version": v.version, "image": v.image,
             "framework": v.framework or "cuda", "entrypoint": v.entrypoint or "",
             "run_command": v.run_command or "", "env": v.env or {},
             "is_default": bool(v.is_default), "created_at": str(v.created_at)}
            for v in versions
        ],
    }


def _validate_custom_name(name: str) -> str:
    name = (name or "").strip().lower()
    if not name or len(name) > 64:
        raise HTTPException(400, "后端名长度须 1-64")
    if not _NAME_RE.match(name):
        raise HTTPException(400, "后端名仅允许小写字母、数字、连字符")
    if not name.endswith(br.CUSTOM_SUFFIX):
        raise HTTPException(400, f"自定义后端名须以 {br.CUSTOM_SUFFIX} 结尾")
    return name


def _version_used_by(db, b: InferenceBackend, version: str) -> list[str]:
    rows = db.query(Deployment).filter(
        Deployment.backend_name == b.name,
        Deployment.backend_version == version).all()
    return [d.name for d in rows]


# ---------- 后端 CRUD ----------

@router.get("/api/inference/backends")
def list_backends(db=Depends(get_db), user=Depends(get_current_user)):
    return [_brief(b, _versions_of(db, b.id))
            for b in db.query(InferenceBackend).order_by(InferenceBackend.id).all()]


class BackendReq(BaseModel):
    name: str
    description: str = ""
    health_check_path: str = "/v1/models"
    default_env: dict = {}
    default_params: dict = {}


@router.post("/api/inference/backends")
def create_backend(req: BackendReq, db: Session = Depends(get_db), user=Depends(get_current_user)):
    name = _validate_custom_name(req.name)
    if db.query(InferenceBackend).filter_by(name=name).first():
        raise HTTPException(400, f"后端 {name} 已存在")
    b = InferenceBackend(
        name=name, source="custom", description=req.description or "",
        health_check_path=(req.health_check_path or "/v1/models").strip() or "/v1/models",
        default_env={str(k): str(v) for k, v in (req.default_env or {}).items()},
        default_params=dict(req.default_params or {}), created_by=user.id)
    db.add(b)
    db.commit()
    db.refresh(b)
    return _brief(b, _versions_of(db, b.id))


@router.put("/api/inference/backends/{bid}")
def update_backend(bid: int, req: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    b = db.get(InferenceBackend, bid)
    if not b:
        raise HTTPException(404, "后端不存在")
    req = {k: v for k, v in (req or {}).items() if v is not None}
    bad = [k for k in req if k not in _EDITABLE]
    if bad:
        raise HTTPException(400, f"不支持的字段: {', '.join(bad)}（后端名不可修改，换名请删除重建）")
    if not req:
        raise HTTPException(400, "无更新字段")
    for k, v in req.items():
        if k in ("default_env", "default_params"):
            setattr(b, k, dict(v or {}))
        elif k == "health_check_path":
            b.health_check_path = (v or "").strip() or "/v1/models"
        else:
            setattr(b, k, v)
    db.commit()
    return _brief(b, _versions_of(db, b.id))


@router.get("/api/inference/backends/{bid}/export")
def export_backend(bid: int, db=Depends(get_db), user=Depends(get_current_user)):
    """导出单后端 YAML（与 import 同格式，供跨环境迁移/备份）。

    builtin 亦可导出（供查阅）；重新导入时 import 校验仅接受 custom 名（-custom 结尾）。"""
    from fastapi.responses import PlainTextResponse
    b = db.get(InferenceBackend, bid)
    if not b:
        raise HTTPException(404, "后端不存在")
    data = {"backends": [{
        "name": b.name,
        "description": b.description or "",
        "health_check_path": b.health_check_path or "/v1/models",
        "default_env": b.default_env or {},
        "default_params": b.default_params or {},
        "versions": [
            {"version": v.version, "image": v.image,
             "framework": v.framework or "cuda", "entrypoint": v.entrypoint or "",
             "run_command": v.run_command or "", "env": v.env or {},
             "is_default": bool(v.is_default)}
            for v in _versions_of(db, bid)
        ],
    }]}
    text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=120)
    return PlainTextResponse(
        text, media_type="application/x-yaml; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{b.name}.yaml"'})


@router.delete("/api/inference/backends/{bid}")
def delete_backend(bid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    b = db.get(InferenceBackend, bid)
    if not b:
        raise HTTPException(404, "后端不存在")
    if b.source == "builtin":
        raise HTTPException(403, "内置后端不可删除")
    used = db.query(Deployment).filter(Deployment.backend_name == b.name).all()
    if used:
        raise HTTPException(409, f"后端被 {len(used)} 个部署引用（{', '.join(d.name for d in used[:5])}…），不可删除")
    db.delete(b)  # 版本随外键级联删除
    db.commit()
    return {"ok": True}


# ---------- 版本管理 ----------

class VersionReq(BaseModel):
    version: str
    image: str
    framework: str = "cuda"
    entrypoint: str = ""
    run_command: str = ""
    env: dict = {}
    is_default: bool = False


@router.post("/api/inference/backends/{bid}/versions")
def add_version(bid: int, req: VersionReq, db: Session = Depends(get_db), user=Depends(get_current_user)):
    b = db.get(InferenceBackend, bid)
    if not b:
        raise HTTPException(404, "后端不存在")
    ver = (req.version or "").strip()
    img = (req.image or "").strip()
    if not ver or len(ver) > 64:
        raise HTTPException(400, "版本号长度须 1-64")
    if not img:
        raise HTTPException(400, "镜像不可为空")
    if db.query(InferenceBackendVersion).filter_by(backend_id=b.id, version=ver).first():
        raise HTTPException(400, f"版本 {ver} 已存在")
    if b.source == "custom" and not (req.run_command or "").strip():
        raise HTTPException(400, "自定义后端的版本必须提供启动命令模板 run_command")
    existing = _versions_of(db, b.id)
    if req.is_default:
        for v in existing:
            v.is_default = False
    db.add(InferenceBackendVersion(
        backend_id=b.id, version=ver, image=img,
        framework=(req.framework or "cuda").strip() or "cuda",
        entrypoint=(req.entrypoint or "").strip(),
        run_command=(req.run_command or "").strip(),
        env={str(k): str(v) for k, v in (req.env or {}).items()},
        is_default=bool(req.is_default) or not existing,
    ))
    db.commit()
    return _brief(b, _versions_of(db, b.id))


@router.put("/api/inference/backends/{bid}/versions/{vid}")
def update_version(bid: int, vid: int, req: dict, db: Session = Depends(get_db), user=Depends(get_current_user)):
    b = db.get(InferenceBackend, bid)
    v = db.get(InferenceBackendVersion, vid)
    if not b or not v or v.backend_id != bid:
        raise HTTPException(404, "版本不存在")
    req = {k: x for k, x in (req or {}).items() if x is not None}
    bad = [k for k in req if k not in _VER_EDITABLE]
    if bad:
        raise HTTPException(400, f"不支持的字段: {', '.join(bad)}")
    if not req:
        raise HTTPException(400, "无更新字段")
    new_ver = str(req.get("version", v.version)).strip()
    if not new_ver or len(new_ver) > 64:
        raise HTTPException(400, "版本号长度须 1-64")
    if new_ver != v.version:
        if db.query(InferenceBackendVersion).filter_by(backend_id=b.id, version=new_ver).first():
            raise HTTPException(400, f"版本 {new_ver} 已存在")
        used = _version_used_by(db, b, v.version)
        if used:
            raise HTTPException(409, f"版本被部署引用（{', '.join(used[:5])}…），不可修改版本号")
    if "image" in req and not str(req["image"] or "").strip():
        raise HTTPException(400, "镜像不可为空")
    if req.get("is_default"):
        for x in _versions_of(db, b.id):
            if x.id != v.id:
                x.is_default = False
    for k, x in req.items():
        if k == "env":
            v.env = {str(a): str(c) for a, c in (x or {}).items()}
        elif isinstance(x, str):
            setattr(v, k, x.strip())
        else:
            setattr(v, k, x)
    db.commit()
    return _brief(b, _versions_of(db, b.id))


@router.delete("/api/inference/backends/{bid}/versions/{vid}")
def delete_version(bid: int, vid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    b = db.get(InferenceBackend, bid)
    v = db.get(InferenceBackendVersion, vid)
    if not b or not v or v.backend_id != bid:
        raise HTTPException(404, "版本不存在")
    if len(_versions_of(db, b.id)) <= 1:
        raise HTTPException(400, "最后一个版本不可删除")
    used = _version_used_by(db, b, v.version)
    if used:
        raise HTTPException(409, f"版本被部署引用（{', '.join(used[:5])}…），不可删除")
    db.delete(v)
    db.commit()
    return {"ok": True}


# ---------- YAML 导入（仅 custom；整批校验、失败全拒绝） ----------

@router.post("/api/inference/backends/import")
def import_backends(file: UploadFile = File(...), db: Session = Depends(get_db),
                    user=Depends(get_current_user)):
    """YAML 导入。格式（三种等价）：
    backends: [ {name, description, health_check_path, default_env, default_params,
                 versions: [{version, image, framework, entrypoint, run_command, env, is_default}]} ]
    或单个后端对象 / 裸列表。导入仅创建（名称已存在即整批拒绝，不覆盖）。"""
    raw = (file.file.read() or b"") if file.file else b""
    try:
        data = yaml.safe_load(raw.decode("utf-8", "ignore"))
    except yaml.YAMLError as e:
        raise HTTPException(400, f"YAML 解析失败: {str(e)[:200]}")
    if isinstance(data, dict) and "backends" in data:
        items = data["backends"] or []
    elif isinstance(data, dict) and "name" in data:
        items = [data]
    elif isinstance(data, list):
        items = data
    else:
        raise HTTPException(400, "YAML 须为 backends: [...] 列表、单个后端对象或裸列表")
    if not items:
        raise HTTPException(400, "导入内容为空")

    errors: list[str] = []
    existing = {b.name for b in db.query(InferenceBackend).all()}
    to_add: list[tuple[InferenceBackend, list[dict], bool]] = []
    for i, it in enumerate(items):
        where = f"第 {i + 1} 项"
        if not isinstance(it, dict):
            errors.append(f"{where}须为对象")
            continue
        try:
            name = _validate_custom_name(str(it.get("name") or ""))
        except HTTPException as e:
            errors.append(f"{where}（{it.get('name') or '无名'}）: {e.detail}")
            continue
        if name in existing:
            errors.append(f"{where}: 后端 {name} 已存在（导入仅创建、不覆盖）")
            continue
        versions = it.get("versions") or []
        if not isinstance(versions, list) or not versions:
            errors.append(f"{where}: versions 不可为空")
            continue
        seen: set[str] = set()
        has_default = False
        v_errors = []
        for j, vt in enumerate(versions):
            if not isinstance(vt, dict):
                v_errors.append(f"version 第 {j + 1} 项须为对象")
                continue
            ver = str(vt.get("version") or "").strip()
            img = str(vt.get("image") or "").strip()
            if not ver or not img:
                v_errors.append(f"version 第 {j + 1} 项 version/image 不可为空")
                continue
            if ver in seen:
                v_errors.append(f"version {ver} 重复")
                continue
            seen.add(ver)
            if not str(vt.get("run_command") or "").strip():
                v_errors.append(f"version {ver} 缺少 run_command（自定义后端必须提供）")
            has_default = has_default or bool(vt.get("is_default"))
        if v_errors:
            errors.append(f"{where}（{name}）: " + "; ".join(v_errors))
            continue
        b = InferenceBackend(
            name=name, source="custom", description=str(it.get("description") or ""),
            health_check_path=str(it.get("health_check_path") or "/v1/models").strip() or "/v1/models",
            default_env={str(k): str(v) for k, v in (it.get("default_env") or {}).items()},
            default_params=dict(it.get("default_params") or {}), created_by=user.id)
        existing.add(name)
        to_add.append((b, versions, has_default))
    if errors:
        raise HTTPException(400, "YAML 导入校验失败: " + " | ".join(errors[:8]))

    created = 0
    for b, versions, has_default in to_add:
        db.add(b)
        db.flush()
        for j, vt in enumerate(versions):
            db.add(InferenceBackendVersion(
                backend_id=b.id,
                version=str(vt["version"]).strip(), image=str(vt["image"]).strip(),
                framework=str(vt.get("framework") or "cuda").strip() or "cuda",
                entrypoint=str(vt.get("entrypoint") or "").strip(),
                run_command=str(vt.get("run_command") or "").strip(),
                env={str(k): str(v) for k, v in (vt.get("env") or {}).items()},
                is_default=bool(vt.get("is_default")) or (not has_default and j == 0),
            ))
            created += 1
        created += 1
    db.commit()
    return {"ok": True, "created": created}

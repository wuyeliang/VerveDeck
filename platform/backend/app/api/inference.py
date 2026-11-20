"""推理部署：vLLM/SGLang 引擎、量化、多 LoRA 热加载、OpenAI 兼容 API 发布（含内容护栏）"""
import re
import time
import logging
import threading

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse, JSONResponse, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db, SessionLocal
from ..security import get_current_user, can_access_project, verify_api_key
from ..models import (Deployment, ModelAsset, Project, User, GuardRule, ApiKey)
from ..services import inference_service as inf
from ..services import prom_service
from ..services import taskcontainer as tc
from ..services import image_ops
from ..services import upgrade_service

log = logging.getLogger("inference")
router = APIRouter(tags=["inference"])

# 启动线程令牌/镜像等待/模型路径解析已收敛到 inference_service
# （API start/create 与调度器自动重启共用同一后台线程实现，行为一致）
def _issue_launch_token(did: int) -> str:
    return inf.issue_launch_token(did)


def _launch_stale(did: int, tok: str) -> bool:
    return inf.launch_stale(did, tok)


def _check(db, pid, user):
    p = db.get(Project, pid)
    if not p or not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")


def _resolve_model_path(asset: ModelAsset, db) -> str:
    return inf.resolve_model_path(asset, db)


def _wait_engine_image(dep: Deployment, db2) -> str:
    return inf.wait_engine_image(dep, db2)


def _served_name(d: Deployment) -> str:
    """引擎实际对外模型名（--served-model-name）：高级参数覆盖，否则回退部署名。
    影响引擎 /v1/models 与平台代理路由，前端代码示例/代理转发据此联动。"""
    adv = getattr(d, "advanced", None) or {}
    return (adv.get("served_model_name") or "").strip() or d.name


def _brief(d: Deployment, db: Session) -> dict:
    asset = db.get(ModelAsset, d.model_asset_id)
    return {
        "id": d.id, "project_id": d.project_id, "name": d.name,
        "model": asset.name if asset else "", "model_asset_id": d.model_asset_id,
        "model_type": asset.type if asset else "",
        "engine": d.engine, "quantization": d.quantization,
        "tensor_parallel": d.tensor_parallel, "max_model_len": d.max_model_len,
        "gpu_mem_util": d.gpu_mem_util,         "enable_lora": d.enable_lora,
        "lora_adapters": d.lora_adapters or [], "replicas": d.replicas,
        "advanced": d.advanced or {},
        # 部署拓扑（#103：PD 分离 / 多级多卡 / RDMA）
        "pd_mode": d.pd_mode or "off",
        "prefill_replicas": d.prefill_replicas or 1, "decode_replicas": d.decode_replicas or 1,
        "prefill_tp": d.prefill_tp or 1, "decode_tp": d.decode_tp or 1,
        "kv_connector": d.kv_connector or "nixl",
        "node_count": d.node_count or 1, "nccl_env": d.nccl_env or {},
        "gpu_need": inf.deployment_gpu_need(d),
        "served_model_name": _served_name(d),
        "endpoint": d.endpoint, "status": d.status, "error": d.error,
        "description": d.description or "",
        "created_at": str(d.created_at),
        "openai_base_url": f"/v1/{d.name}" if d.status == "running" else "",
        # 推理重构（实例运维/网关/后端注册表/扩展 KV）
        "state_message": d.state_message or "",
        "restart_on_error": bool(d.restart_on_error),
        "error_count": d.error_count or 0,
        "next_restart_at": str(d.next_restart_at) if d.next_restart_at else "",
        "kv_cache_ext": d.kv_cache_ext or {},
        "gateway_port": d.gateway_port or 0,
        "backend_name": d.backend_name or "",
        "backend_version": d.backend_version or "",
        "cache_service_id": d.cache_service_id,
    }


# ---------- 部署管理 ----------

class DeployReq(BaseModel):
    project_id: int
    name: str
    model_asset_id: int
    engine: str = "vllm"
    quantization: str = "none"
    tensor_parallel: int = 1
    max_model_len: int = 8192
    gpu_mem_util: float = 0.85
    enable_lora: bool = False
    lora_adapters: list[dict] = []
    replicas: int = 1
    description: str = ""
    advanced: dict = {}  # 引擎高级参数（留空=引擎默认），见 Deployment.advanced
    # ---------- 部署拓扑：PD 分离 / 多级多卡 / RDMA（#103） ----------
    pd_mode: str = "off"  # off=协同 / pd=Prefill-Decode 分离
    prefill_replicas: int = 1  # PD：prefill 实例数
    decode_replicas: int = 1  # PD：decode 实例数
    prefill_tp: int = 1  # PD：每 prefill 实例卡数
    decode_tp: int = 1  # PD：每 decode 实例卡数
    kv_connector: str = "nixl"  # PD KV 传输：nixl / mooncake
    node_count: int = 1  # 实例池跨逻辑节点数（放置分组；单实例单机闭环）
    nccl_env: dict = {}  # NCCL/UCX/NIXL 环境变量覆盖
    # ---------- 推理重构（后端注册表/缓存加速） ----------
    backend_name: str | None = None  # 推理后端（空=builtin 引擎；自定义须以 -custom 结尾）
    backend_version: str | None = None  # 后端版本（空=默认版本）
    cache_service_id: int | None = None  # 关联缓存加速服务（实例注入 VD_CACHE_ENDPOINT）


@router.post("/api/inference/deployments")
def create_deploy(req: DeployReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check(db, req.project_id, user)
    if req.engine not in ("vllm", "sglang"):
        raise HTTPException(400, "engine 需为 vllm/sglang")
    if db.query(Deployment).filter_by(name=req.name).first():
        raise HTTPException(400, "部署名已存在")
    from ..models import ModelRoute
    if db.query(ModelRoute).filter_by(name=req.name).first():
        raise HTTPException(400, "与已有模型路由名冲突（/v1/ 路径前缀不能碰撞）")
    asset = db.get(ModelAsset, req.model_asset_id)
    if not asset or asset.status != "ready":
        raise HTTPException(400, "模型资产不存在或未就绪")
    # 拓扑校验（#103：PD 分离 / 多级多卡）
    pd_mode = req.pd_mode if req.pd_mode in ("off", "pd") else "off"
    if pd_mode == "pd" and req.kv_connector not in ("nixl", "mooncake"):
        raise HTTPException(400, "kv_connector 需为 nixl/mooncake")
    if pd_mode == "pd":
        if min(req.prefill_replicas, req.decode_replicas, req.prefill_tp, req.decode_tp) < 1:
            raise HTTPException(400, "PD 分离：各角色实例数/卡数均需 ≥1")
        need = max(1, req.prefill_replicas) * max(1, req.prefill_tp) \
            + max(1, req.decode_replicas) * max(1, req.decode_tp)
    else:
        need = max(1, req.tensor_parallel) * max(1, req.replicas)
    # 推理后端 / 缓存服务选型校验（自定义后端须存在且不可 PD；builtin 归一为空）
    from ..services import backend_registry as br
    from ..models import CacheService
    try:
        backend_name, backend_version = br.validate_choice(
            db, req.backend_name, req.backend_version, pd_mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    cache_service_id = req.cache_service_id
    if cache_service_id and not db.get(CacheService, cache_service_id):
        raise HTTPException(400, "关联的缓存服务不存在")
    # GPU 准入预检（fail-fast；容器创建时 taskcontainer 再做物理空闲卡分配兜底）
    from ..models import GpuNode
    from .. import scheduler
    node = db.query(GpuNode).filter_by(name=settings.node_name).first()
    if node and need > (node.gpu_count or 0) - scheduler._gpu_in_use(db):
        raise HTTPException(400, f"GPU 不足：需要 {need} 卡，可用 "
                                 f"{max(0, (node.gpu_count or 0) - scheduler._gpu_in_use(db))}")
    d = Deployment(
        project_id=req.project_id, name=req.name, model_asset_id=asset.id,
        engine=req.engine, quantization=req.quantization, tensor_parallel=req.tensor_parallel,
        max_model_len=req.max_model_len, gpu_mem_util=req.gpu_mem_util,
        enable_lora=req.enable_lora, lora_adapters=req.lora_adapters,
        replicas=max(1, req.replicas), description=req.description,
        advanced=req.advanced or {}, status="creating", created_by=user.id,
        # 部署拓扑（PD 分离 / 多级多卡 / RDMA）
        pd_mode=pd_mode,
        prefill_replicas=max(1, req.prefill_replicas), decode_replicas=max(1, req.decode_replicas),
        prefill_tp=max(1, req.prefill_tp), decode_tp=max(1, req.decode_tp),
        kv_connector=req.kv_connector if req.kv_connector in ("nixl", "mooncake") else "nixl",
        node_count=max(1, req.node_count),
        nccl_env={str(k): str(v) for k, v in (req.nccl_env or {}).items()},
        # 推理重构（后端注册表/缓存加速）
        backend_name=backend_name, backend_version=backend_version,
        cache_service_id=cache_service_id,
    )
    db.add(d)
    db.commit()
    db.refresh(d)

    inf.launch_thread(d.id, note="推理容器启动中…")
    return _brief(d, db)


class PreviewReq(BaseModel):
    """启动配置预览（不建部署）：参数面与 DeployReq 一致；name/model 可缺省用占位符"""
    name: str = ""
    model_asset_id: int | None = None
    engine: str = "vllm"
    quantization: str = "none"
    tensor_parallel: int = 1
    max_model_len: int = 8192
    gpu_mem_util: float = 0.85
    enable_lora: bool = False
    lora_adapters: list[dict] = []
    replicas: int = 1
    advanced: dict = {}
    pd_mode: str = "off"
    prefill_replicas: int = 1
    decode_replicas: int = 1
    prefill_tp: int = 1
    decode_tp: int = 1
    kv_connector: str = "nixl"
    node_count: int = 1
    nccl_env: dict = {}
    backend_name: str | None = None
    backend_version: str | None = None
    cache_service_id: int | None = None
    kv_cache_ext: dict = {}


@router.post("/api/inference/deployments/preview")
def preview_deploy(req: PreviewReq, db=Depends(get_db), user=Depends(get_current_user)):
    """按当前表单拼真实启动命令（与 build_cmd 同一代码路径，保证预览 == 实际启动）。
    运行时才确定的值（PD 端口基于部署 id、worker 容器 IP）以占位符展示。"""
    from types import SimpleNamespace
    import shlex as _shlex
    from ..services import backend_registry as br
    from ..services import cache_service as cs
    if req.engine not in ("vllm", "sglang"):
        raise HTTPException(400, "engine 需为 vllm/sglang")
    pd_mode = req.pd_mode if req.pd_mode in ("off", "pd") else "off"
    try:
        backend_name, backend_version = br.validate_choice(
            db, req.backend_name, req.backend_version, pd_mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    asset = db.get(ModelAsset, req.model_asset_id) if req.model_asset_id else None
    model_path = (inf.resolve_model_path(asset, db) or "") if asset else ""
    model_path = model_path or "<model_path>"
    dep = SimpleNamespace(
        id=0, name=req.name or "<部署名>",
        engine=req.engine, quantization=req.quantization,
        tensor_parallel=max(1, req.tensor_parallel or 1),
        max_model_len=req.max_model_len, gpu_mem_util=req.gpu_mem_util,
        enable_lora=bool(req.enable_lora), lora_adapters=req.lora_adapters or [],
        advanced=req.advanced or {},
        pd_mode=pd_mode,
        prefill_replicas=max(1, req.prefill_replicas or 1),
        decode_replicas=max(1, req.decode_replicas or 1),
        prefill_tp=max(1, req.prefill_tp or 1), decode_tp=max(1, req.decode_tp or 1),
        kv_connector=req.kv_connector if req.kv_connector in ("nixl", "mooncake") else "nixl",
        node_count=max(1, req.node_count or 1),
        nccl_env={str(k): str(v) for k, v in (req.nccl_env or {}).items()},
        kv_cache_ext=req.kv_cache_ext or {},
        backend_name=backend_name, backend_version=backend_version,
        cache_service_id=req.cache_service_id,
    )
    try:
        if br.is_custom_name(backend_name):
            bc = br.backend_context(backend_name, backend_version)
            image = bc["image"]
        else:
            bc = {}
            image = upgrade_service.current_image("vllm") if req.engine == "vllm" \
                else upgrade_service.current_image("sglang")
    except Exception as e:  # RuntimeError（后端/版本缺失）等 → 400
        raise HTTPException(400, str(e))
    base_env = bc.get("env") or {"VLLM_LOGGING_LEVEL": "warning",
                                 "VLLM_ALLOW_RUNTIME_LORA_UPDATING": "1"}
    env = {**base_env, **dep.nccl_env, **cs.cache_env_for(dep)}

    def _fmt(argv: list[str]) -> str:
        # vLLM/自定义 = bash -lc 脚本串（去掉尾部 2>&1）；SGLang = argv 加引号拼接
        if len(argv) >= 3 and argv[0] == "bash" and argv[1] == "-lc":
            return argv[2].removesuffix(" 2>&1")
        return " ".join(_shlex.quote(a) for a in argv)

    cmds: list[dict] = []
    if pd_mode == "pd" and not br.is_custom_name(backend_name):
        cmds.append({"label": "prefill p0", "gpus": dep.prefill_tp,
                     "cmd": _fmt(inf.build_cmd(dep, model_path, role="prefill", idx=0))})
        cmds.append({"label": "decode d0", "gpus": dep.decode_tp,
                     "cmd": _fmt(inf.build_cmd(dep, model_path, role="decode", idx=0))})
    else:
        cmds.append({"label": "replica r0", "gpus": dep.tensor_parallel,
                     "cmd": _fmt(inf.build_cmd(dep, model_path,
                                               worker_ip="<worker_ip>" if bc else ""))})
    return {"image": image, "model_path": model_path, "env": env, "commands": cmds}


@router.get("/api/inference/deployments")
def list_deploys(project_id: int | None = None, db=Depends(get_db), user=Depends(get_current_user)):
    if project_id:
        _check(db, project_id, user)
        q = db.query(Deployment).filter(Deployment.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = db.query(Deployment).filter(Deployment.project_id.in_(pids or [0]))
    rows = q.order_by(Deployment.id.desc()).all()
    return [_brief(d, db) for d in rows]


@router.get("/api/inference/deployments/{did}")
def get_deploy(did: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    return _brief(d, db)


@router.post("/api/inference/deployments/{did}/start")
def start_deploy(did: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    # 人工启动即干预：清零自动重启退避（下次故障从 30s 重新开始）
    if d.error_count:
        d.error_count = 0
        d.next_restart_at = None
        db.commit()
    inf.launch_thread(d.id, note="推理容器启动中…")
    return {"ok": True}


@router.post("/api/inference/deployments/{did}/stop")
def stop_deploy(did: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    _issue_launch_token(d.id)  # 作废旧 start 线程，防止其超时回写覆盖 stopped
    inf.stop(d)
    d = db.get(Deployment, did)
    d.status = "stopped"
    db.commit()
    return {"ok": True}


@router.delete("/api/inference/deployments/{did}")
def delete_deploy(did: int, db=Depends(get_db), user=Depends(get_current_user)):
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    if d.status in ("running", "creating"):
        inf.stop(d)
    db.delete(d)
    db.commit()
    return {"ok": True}


class BatchDeleteReq(BaseModel):
    ids: list[int]


@router.post("/api/inference/deployments/batch-delete")
def batch_delete_deploys(req: BatchDeleteReq, db=Depends(get_db), user=Depends(get_current_user)):
    """批量删除部署：运行中/创建中的先停止容器再删除，返回部分结果"""
    deleted: list[int] = []
    failed: list[dict] = []
    for did in req.ids:
        try:
            delete_deploy(did, db=db, user=user)
            deleted.append(did)
        except HTTPException as e:
            db.rollback()
            failed.append({"id": did, "error": str(e.detail)})
    return {"deleted": deleted, "failed": failed}


@router.post("/api/inference/deployments/{did}/scale")
def scale_deploy(did: int, req: dict, db=Depends(get_db), user=Depends(get_current_user)):
    """弹性扩缩容：调整副本数（需空闲 GPU）。PD 部署请走 /scale-role（按 P/D 角色）。"""
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    if (d.pd_mode or "off") == "pd":
        raise HTTPException(400, "PD 部署请用角色级扩缩容（/scale-role，role=prefill|decode）")
    from ..models import GpuNode, TrainingJob
    from .. import scheduler
    node = db.query(GpuNode).filter_by(name=settings.node_name).first()
    used = scheduler._gpu_in_use(db)
    want = max(1, int(req.get("replicas", d.replicas)))
    if want == d.replicas:
        # 幂等：副本数不变为 no-op，直接返回（不做 GPU 复检，避免瞬态占用误报 400）
        return _brief(d, db)
    need = want * (d.tensor_parallel or 1)
    have = d.replicas * (d.tensor_parallel or 1)
    if node and need > (node.gpu_count or 0) - used + have:
        raise HTTPException(400, f"GPU 不足：需要 {need} 卡，可用 {max(0, (node.gpu_count or 0) - used + have)}")
    asset = db.get(ModelAsset, d.model_asset_id)
    path = _resolve_model_path(asset, db)
    try:
        inf.ensure_deployment_model(asset, db)  # 单副本：本地缓存缺失从桶拉取
    except Exception as e:
        raise HTTPException(503, str(e))
    old = d.replicas
    # 扩容：先建新副本（容器一旦创建即 docker-alive），再登记并提交——
    # 避免并发 reconcile 在"新副本尚未登记"的窗口里按旧 container_ids 判死。
    if want > old:
        for i in range(old, want):
            inf.start_replica(d, i, path)
    d.replicas = want
    d.container_ids = [inf.deployment_container_name(d, i) for i in range(want)]
    db.commit()
    # 缩容：先把新的（更小的）container_ids 落库提交，再摘除多余副本。
    # 若先删容器后提交，并发 reconcile 会读到"旧表仍含已删副本"，
    # 误判 not-alive → 把仍健康的部署标成 lost→failed（竞态）。
    if want < old:
        for i in range(old - 1, want - 1, -1):
            tc.remove_container(inf.deployment_container_name(d, i), force=True)
    # 扩缩容后容器 IP 变化，刷新 Prometheus file_sd target
    tgs = []
    for i in range(want):
        ip = tc.container_ip(inf.deployment_container_name(d, i))
        if ip:
            tgs.append(f"{ip}:8000")
    if tgs:
        prom_service.write_infer_sd(d.id, d.engine, inf.deployment_container_name(d, 0), tgs)
    return _brief(d, db)


def _pd_rebuild_ids(d: Deployment) -> list:
    """PD container_ids 归整（[p0..pA, d0..dB, (rt 仅 SGLang)]；按当前容器实况重建）"""
    ids = []
    for i in range(max(1, d.prefill_replicas or 1)):
        ids.append(inf.pd_instance_name(d, "prefill", i))
    for j in range(max(1, d.decode_replicas or 1)):
        ids.append(inf.pd_instance_name(d, "decode", j))
    if d.engine == "sglang":
        ids.append(inf.pd_router_name(d))
    return ids


@router.post("/api/inference/deployments/{did}/scale-role")
def scale_role_deploy(did: int, req: dict, db=Depends(get_db), user=Depends(get_current_user)):
    """PD 部署角色级扩缩容：{role: prefill|decode, replicas: N}（需空闲 GPU）"""
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    if (d.pd_mode or "off") != "pd":
        raise HTTPException(400, "该部署非 PD 模式，请用 /scale")
    role = str(req.get("role", ""))
    if role not in ("prefill", "decode"):
        raise HTTPException(400, "role 需为 prefill/decode")
    cur = d.prefill_replicas if role == "prefill" else d.decode_replicas
    want = max(1, int(req.get("replicas", cur)))
    if want == cur:
        return _brief(d, db)
    from ..models import GpuNode
    from .. import scheduler
    node = db.query(GpuNode).filter_by(name=settings.node_name).first()
    used = scheduler._gpu_in_use(db)
    have = inf.deployment_gpu_need(d)
    need = have + (want - cur) * (d.prefill_tp if role == "prefill" else d.decode_tp)
    if node and need > (node.gpu_count or 0) - used + have:
        raise HTTPException(400, f"GPU 不足：需要 {need} 卡，可用 {max(0, (node.gpu_count or 0) - used + have)}")
    asset = db.get(ModelAsset, d.model_asset_id)
    path = _resolve_model_path(asset, db)
    try:
        inf.ensure_deployment_model(asset, db)  # 单副本：本地缓存缺失从桶拉取
    except Exception as e:
        raise HTTPException(503, str(e))
    if want > cur:
        # 扩容：先建新实例（docker-alive），SGLang router 需感知新 prefill → 重建
        for i in range(cur, want):
            inf.start_pd_instance(d, role, i, path)
        if d.engine == "sglang":
            inf.start_pd_router(d, path)
    # 落库（先登记新表再删旧实例，避免 reconcile 竞态——与协同缩容同口径）
    if role == "prefill":
        d.prefill_replicas = want
    else:
        d.decode_replicas = want
    d.container_ids = _pd_rebuild_ids(d)
    db.commit()
    if want < cur:
        for i in range(cur - 1, want - 1, -1):
            tc.remove_container(inf.pd_instance_name(d, role, i), force=True)
    # 刷新 Prometheus target（全部引擎实例）
    tgs = [f"{tc.container_ip(c)}:8000" for c in d.container_ids if tc.container_ip(c)]
    if tgs:
        prom_service.write_infer_sd(d.id, d.engine, inf.pd_instance_name(d, "prefill", 0), tgs)
    # vLLM PD 入口=首个 D 实例：D 池变化时同步 endpoint
    if d.engine == "vllm" and role == "decode":
        d0 = tc.container_ip(d.container_ids[max(1, d.prefill_replicas or 1)]) \
            if len(d.container_ids) > max(1, d.prefill_replicas or 1) else ""
        if d0:
            d.endpoint = f"{d0}:8000"
            db.commit()
    return _brief(d, db)


# ---------- 部署编辑（即时字段直改 / 参数字段自动重建生效） ----------

_IMMEDIATE_FIELDS = {"name", "description", "restart_on_error",
                     "backend_name", "backend_version"}


@router.put("/api/inference/deployments/{did}")
def update_deploy(did: int, req: dict, db=Depends(get_db), user=Depends(get_current_user)):
    """编辑部署：name/description/restart_on_error/backend 即时生效；
    其余参数（引擎/副本/拓扑/高级参数/扩展 KV 等）自动停旧实例重建生效
    （状态 → creating，后台线程完成重建）。"""
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    # 外键字段显式传 null = 清除关联（通用 None 过滤会吞掉该语义，需特判）
    clear_cache = (req or {}).get("cache_service_id") is None and "cache_service_id" in (req or {})
    req = {k: v for k, v in (req or {}).items() if v is not None}
    if clear_cache:
        req["cache_service_id"] = None
    if not req:
        raise HTTPException(400, "无更新字段")
    if "name" in req and req["name"] != d.name:
        if db.query(Deployment).filter_by(name=req["name"]).first():
            raise HTTPException(400, "部署名已存在")
        from ..models import ModelRoute
        if db.query(ModelRoute).filter_by(name=req["name"]).first():
            raise HTTPException(400, "与已有模型路由名冲突")
    if "model_asset_id" in req:
        asset = db.get(ModelAsset, req["model_asset_id"])
        if not asset or asset.status != "ready":
            raise HTTPException(400, "模型资产不存在或未就绪")

    rebuild = any(k not in _IMMEDIATE_FIELDS for k in req)
    # 参数校验（与创建同口径）
    engine = req.get("engine", d.engine)
    if engine not in ("vllm", "sglang"):
        raise HTTPException(400, "engine 需为 vllm/sglang")
    pd_mode = req.get("pd_mode", d.pd_mode or "off")
    if pd_mode not in ("off", "pd"):
        raise HTTPException(400, "pd_mode 需为 off/pd")
    if pd_mode == "pd" and \
            req.get("kv_connector", d.kv_connector or "nixl") not in ("nixl", "mooncake"):
        raise HTTPException(400, "kv_connector 需为 nixl/mooncake")
    # 推理后端 / 缓存服务选型校验（与创建同口径；空串=清除自定义后端/取消关联）
    from ..services import backend_registry as br
    from ..models import CacheService
    try:
        backend_name, backend_version = br.validate_choice(
            db, req.get("backend_name", d.backend_name),
            req.get("backend_version", d.backend_version), pd_mode)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if "backend_name" in req or "backend_version" in req:
        # 归一化写回（builtin 选择/空串 → None，与创建口径一致）
        req["backend_name"] = backend_name
        req["backend_version"] = backend_version
    if "cache_service_id" in req and req["cache_service_id"] \
            and not db.get(CacheService, req["cache_service_id"]):
        raise HTTPException(400, "关联的缓存服务不存在")
    # GPU 需求增量预检（重建后需求变大才查容量）
    new_need = (max(1, req.get("prefill_replicas", d.prefill_replicas or 1))
                * max(1, req.get("prefill_tp", d.prefill_tp or 1))
                + max(1, req.get("decode_replicas", d.decode_replicas or 1))
                * max(1, req.get("decode_tp", d.decode_tp or 1))) \
        if pd_mode == "pd" \
        else max(1, req.get("tensor_parallel", d.tensor_parallel)) \
        * max(1, req.get("replicas", d.replicas))
    if rebuild and new_need > inf.deployment_gpu_need(d):
        from ..models import GpuNode
        from .. import scheduler
        node = db.query(GpuNode).filter_by(name=settings.node_name).first()
        used = scheduler._gpu_in_use(db)
        have = inf.deployment_gpu_need(d)
        if node and new_need > (node.gpu_count or 0) - used + have:
            raise HTTPException(400, f"GPU 不足：需要 {new_need} 卡，"
                                     f"可用 {max(0, (node.gpu_count or 0) - used + have)}")
    # 应用字段
    for k, v in req.items():
        if k == "nccl_env":
            setattr(d, k, {str(a): str(b) for a, b in (v or {}).items()})
        elif k in ("kv_cache_ext", "advanced"):
            setattr(d, k, dict(v or {}))
        elif k == "lora_adapters":
            setattr(d, k, list(v or []))
        else:
            setattr(d, k, v)
    if "restart_on_error" in req and not req["restart_on_error"]:
        d.next_restart_at = None  # 关闭自动重启：取消已计划的退避重启
    if not rebuild:
        db.commit()
        return _brief(d, db)
    # 参数变更：清旧实例 → 后台重建（镜像→容器→就绪→running）
    if d.status in ("running", "creating"):
        inf.cleanup_instances(d)
    d.status = "creating"
    d.error = ""
    d.next_restart_at = None
    d.state_message = "参数变更，重建实例中…"
    db.commit()
    inf.launch_thread(d.id, note="参数变更，重建实例中…")
    return _brief(d, db)


# ---------- 实例视角（单实例状态 / 删除重建） ----------

@router.get("/api/inference/deployments/{did}/instances")
def list_instances(did: int, db=Depends(get_db), user=Depends(get_current_user)):
    """实例视角：单实例列表（协同副本 / PD P 池·D 池 / SGLang router）与存活状态"""
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    items = []
    if (d.pd_mode or "off") == "pd":
        for i in range(max(1, d.prefill_replicas or 1)):
            name = inf.pd_instance_name(d, "prefill", i)
            items.append({"role": "prefill", "index": i, "name": name,
                          "alive": tc.container_alive(name), "ip": tc.container_ip(name) or ""})
        for j in range(max(1, d.decode_replicas or 1)):
            name = inf.pd_instance_name(d, "decode", j)
            items.append({"role": "decode", "index": j, "name": name,
                          "alive": tc.container_alive(name), "ip": tc.container_ip(name) or ""})
        if d.engine == "sglang":
            name = inf.pd_router_name(d)
            items.append({"role": "router", "index": 0, "name": name,
                          "alive": tc.container_alive(name), "ip": tc.container_ip(name) or ""})
    else:
        for i in range(max(1, d.replicas or 1)):
            name = inf.deployment_container_name(d, i)
            items.append({"role": "replica", "index": i, "name": name,
                          "alive": tc.container_alive(name), "ip": tc.container_ip(name) or ""})
    return {"status": d.status, "state_message": d.state_message or "",
            "restart_on_error": bool(d.restart_on_error), "error_count": d.error_count or 0,
            "next_restart_at": str(d.next_restart_at) if d.next_restart_at else "",
            "instances": items}


@router.post("/api/inference/deployments/{did}/instances/rebuild")
def rebuild_instance(did: int, req: dict, db=Depends(get_db), user=Depends(get_current_user)):
    """单实例重建：删除指定实例并重建（部署级状态不变，实例引擎就绪后自动回服务）"""
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    if d.status not in ("running", "creating"):
        raise HTTPException(400, "仅运行中/创建中的部署可重建实例")
    asset = db.get(ModelAsset, d.model_asset_id)
    path = _resolve_model_path(asset, db)
    try:
        inf.ensure_deployment_model(asset, db)  # 单副本：本地缓存缺失从桶拉取
    except Exception as e:
        raise HTTPException(503, str(e))
    role = str(req.get("role", "r"))
    index = int(req.get("index", 0))
    if (d.pd_mode or "off") == "pd":
        if role not in ("p", "d", "rt"):
            raise HTTPException(400, "role 需为 p/d/rt")
        if role == "rt":
            if d.engine != "sglang":
                raise HTTPException(400, "router 实例仅存在于 SGLang PD 部署")
            inf.start_pd_router(d, path)
        else:
            rr = "prefill" if role == "p" else "decode"
            cur = d.prefill_replicas if role == "p" else d.decode_replicas
            if not (0 <= index < max(1, cur or 1)):
                raise HTTPException(400, "实例下标越界")
            inf.start_pd_instance(d, rr, index, path)
            if role == "p" and d.engine == "sglang":
                inf.start_pd_router(d, path)  # SGLang router 需感知 prefill 池变化
            # vLLM PD 入口=首个 D 实例：重建后入口 IP 可能变化
            if role == "d" and d.engine == "vllm" and index == 0:
                d0 = tc.container_ip(inf.pd_instance_name(d, "decode", 0))
                if d0:
                    d.endpoint = f"{d0}:8000"
                    db.commit()
        tgs = [f"{tc.container_ip(c)}:8000" for c in (d.container_ids or []) if tc.container_ip(c)]
        first = inf.pd_instance_name(d, "prefill", 0)
    else:
        if not (0 <= index < max(1, d.replicas or 1)):
            raise HTTPException(400, "实例下标越界")
        inf.start_replica(d, index, path)
        if index == 0 and not d.endpoint:
            ip = tc.container_ip(inf.deployment_container_name(d, 0))
            if ip:
                d.endpoint = f"{ip}:8000"
                db.commit()
        tgs = [f"{tc.container_ip(inf.deployment_container_name(d, i))}:8000"
               for i in range(max(1, d.replicas or 1))
               if tc.container_ip(inf.deployment_container_name(d, i))]
        first = inf.deployment_container_name(d, 0)
    if tgs:
        prom_service.write_infer_sd(d.id, d.engine, first, tgs)
    return _brief(d, db)


@router.get("/api/inference/deployments/{did}/logs")
def deploy_logs(did: int, tail: int = 200, replica: int = 0, role: str = "r",
                db=Depends(get_db), user=Depends(get_current_user)):
    """部署日志。role：r=协同副本 / p=PD prefill / d=PD decode / rt=SGLang router"""
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    import os
    tag = role if role in ("p", "d", "rt") else "r"
    p = os.path.join(settings.data_dir, "infer", str(did), f"run-{tag}{replica}.log")
    if not os.path.exists(p):
        return {"logs": ""}
    with open(p, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - 60000))
        data = f.read().decode("utf-8", "ignore")
    return {"logs": "\n".join(data.splitlines()[-tail:])}


@router.get("/api/inference/deployments/{did}/metrics")
def deploy_metrics(did: int, minutes: int = 60, db=Depends(get_db), user=Depends(get_current_user)):
    """推理引擎监控（KV 缓存占用 / 运行中·排队请求 / P95 延迟）
    指标由 Prometheus 经 docker_sd 直接抓引擎 /metrics（按 deployment_id 标签过滤），保留 15d"""
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    sel = f'{{deployment_id="{did}"}}'
    exprs = [
        ("kv", f"max(vllm:kv_cache_usage_perc{sel} or sglang:token_usage{sel}) * 100"),
        ("running", f"max(vllm:num_requests_running{sel} or sglang:num_running_requests{sel})"),
        ("waiting", f"max(vllm:num_requests_waiting{sel} or sglang:num_queue_requests{sel})"),
        ("latency_ms",
         f"histogram_quantile(0.95, sum by (le) "
         f"(rate(vllm:e2e_request_latency_seconds_bucket{sel}[5m]))) * 1000"),
    ]
    per_ts: dict[int, dict] = {}
    for key, expr in exprs:
        try:
            results = prom_service.prom_range(expr, minutes=minutes, step="30s")
        except Exception:
            continue
        for r in results:
            for ts, v in r.get("values", []):
                t = int(float(ts))
                per_ts.setdefault(t, {"ts": t})[key] = prom_service._f(v)
    return {"engine": d.engine, "series": [per_ts[t] for t in sorted(per_ts)]}


# ---------- 多 LoRA 动态热加载 ----------

@router.post("/api/inference/deployments/{did}/adapters")
def add_adapter(did: int, req: dict, db=Depends(get_db), user=Depends(get_current_user)):
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    name = req.get("name", f"lora-{int(time.time())}")
    path = req.get("path") or ""
    if not path:
        aid = req.get("asset_id")
        a = db.get(ModelAsset, aid) if aid else None
        if not a:
            raise HTTPException(400, "需 path 或 asset_id")
        path = a.path
    res = inf.hot_add_lora(d, name, path)
    if res.get("ok"):
        d.lora_adapters = [a for a in (d.lora_adapters or []) if a.get("name") != name]
        d.lora_adapters.append({"name": name, "path": path})
        d.enable_lora = True
        db.commit()
    return res


@router.delete("/api/inference/deployments/{did}/adapters/{name}")
def remove_adapter(did: int, name: str, db=Depends(get_db), user=Depends(get_current_user)):
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    res = inf.hot_remove_lora(d, name)
    d.lora_adapters = [a for a in (d.lora_adapters or []) if a.get("name") != name]
    db.commit()
    return res


# ---------- 内容护栏 ----------

def _check_guard(db: Session, project_id: int | None, text: str, scope: str) -> str | None:
    if not text:
        return None
    rules = db.query(GuardRule).filter(GuardRule.enabled == True).all()
    for r in rules:
        if r.project_id not in (None, project_id):
            continue
        if r.scope not in (scope, "both"):
            continue
        try:
            if re.search(r.pattern, text, re.I):
                return f"命中护栏规则 #{r.id}: {r.description or r.pattern[:40]}"
        except re.error:
            continue
    return None


# ---------- OpenAI 兼容代理（含认证 + 护栏） ----------

@router.api_route("/v1/{deploy_name}/{rest:path}", methods=["POST", "GET"])
async def openai_proxy(deploy_name: str, rest: str, request: Request, db=Depends(get_db)):
    """存量直连入口（catch-all 兜底路由 → backend:3001）：按路径解析部署后走代理核心"""
    dep = db.query(Deployment).filter_by(name=deploy_name).first()
    if not dep:
        raise HTTPException(404, "部署不存在")
    return await _proxy_core(dep, rest, request, db)


async def _proxy_core(dep: Deployment, rest: str, request: Request, db: Session):
    """代理核心（openai_proxy 与网关目标代理 target_proxy 共用）：
    状态检查 → 认证（x-api-key / Bearer JWT）→ model 名归一 → 输入护栏
    → PD 二阶段或单目标转发 → 输出护栏。"""
    if dep.status != "running" or not dep.endpoint:
        raise HTTPException(503, "部署未运行")
    # 认证：x-api-key（项目 key）或 Bearer JWT
    authed = False
    project_id = dep.project_id
    xkey = request.headers.get("x-api-key", "")
    if xkey:
        k = verify_api_key(db, project_id, xkey)
        if k:
            # 模型范围校验：key 限定了模型时，部署所绑定的模型必须在授权列表内
            scope = k.model_ids or []
            if scope and dep.model_asset_id not in scope:
                raise HTTPException(403, "该 API Key 未授权访问此模型")
            authed = True
    else:
        auth = request.headers.get("authorization", "")
        if auth.startswith("Bearer "):
            try:
                from ..security import decode_token
                payload = decode_token(auth[7:])
                user = db.get(User, int(payload["sub"]))
                if user and user.is_active and can_access_project(user, db, db.get(Project, project_id)):
                    authed = True
            except Exception:
                pass
    if not authed:
        raise HTTPException(401, "认证失败：使用 x-api-key 或 Bearer JWT")

    body = await request.body()
    # 联动：高级参数 served_model_name 决定引擎 /v1/models 与接受的 model 名。
    # 平台代理按 deploy_name 路由，转发前把请求体 model 归一到引擎实际模型名，
    # 客户端可继续用 /v1/<部署名> 路径（存量部署 served==部署名，此处为 no-op）。
    import json as _json
    payload = {}
    served = _served_name(dep)
    try:
        payload = _json.loads(body) if body else {}
        if isinstance(payload, dict) and payload.get("model") and payload.get("model") != served:
            payload["model"] = served
            body = _json.dumps(payload).encode()
    except Exception:
        payload = {}
    # 输入护栏
    try:
        msgs = (payload.get("messages") or []) if isinstance(payload, dict) else []
        in_text = " ".join(m.get("content", "") for m in msgs if isinstance(m, dict))
        hit = _check_guard(db, project_id, in_text, "input")
        if hit:
            return JSONResponse(status_code=451, content={"error": {"message": f"内容安全护栏拦截: {hit}"}})
    except Exception:
        pass

    # PD 分离（vLLM）：两阶段路由内置 backend（P 算 prefill 取 KV 参数 → D 带参生成）；
    # 非生成类子路径（/models 等）与 SGLang PD（入口=sglang_router 容器）走下方单目标转发
    if (dep.pd_mode or "off") == "pd" and dep.engine == "vllm" \
            and (rest.endswith("chat/completions") or rest.endswith("completions")):
        return await _pd_two_stage(dep, rest, payload, request, db, project_id, dep.name)

    # 对外路径 /v1/<deploy_name>/<rest>，vLLM/SGLang 的 OpenAI 兼容路由统一在 /v1/ 下，转发需补前缀
    target = f"http://{dep.endpoint}/v1/{rest}"
    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ("content-type", "authorization", "x-api-key")}
    client = httpx.AsyncClient(timeout=httpx.Timeout(300, connect=10))
    try:
        areq = client.build_request(request.method, target, content=body, headers=fwd_headers)
        resp = await client.send(areq, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"代理转发失败: {e}")
    return await _finish_forward(resp, client, db, project_id, dep.name, rest, payload)


# ---------- 转发收尾（协同单目标 与 PD 二阶段共用） ----------

async def _finish_forward(resp, client, db, project_id: int, deploy_name: str,
                          rest: str, payload: dict):
    """引擎响应 → 客户端响应：非流式读整段+输出护栏；流式 SSE 透传+后置护栏"""
    import json as _json

    def _guard_log(text: str):
        hit = _check_guard(db, project_id, text, "output")
        if hit:
            log.warning("guardrail output hit deploy=%s: %s", deploy_name, hit)

    is_chat = rest.endswith("chat/completions")
    is_stream = bool(payload.get("stream")) if isinstance(payload, dict) else False

    if is_chat and not is_stream:
        # 非流式：在 endpoint 内读完整段再返回，client 可立即安全关闭
        content_bytes = b""
        try:
            async for chunk in resp.aiter_bytes():
                content_bytes += chunk
        finally:
            await resp.aclose()
            await client.aclose()
        try:
            out = _json.loads(content_bytes)
            _guard_log(out.get("choices", [{}])[0].get("message", {}).get("content", ""))
            return JSONResponse(status_code=resp.status_code, content=out)
        except Exception:
            # 上游非 JSON 错误体（如 400/500 文本），原样透传
            return Response(content=content_bytes, status_code=resp.status_code,
                            media_type=resp.headers.get("content-type", "application/json"))

    async def stream_gen():
        # 流式：body 尚未消费，client 必须等 generator 结束后再关（endpoint 的 finally 会提前执行）
        buf = []
        try:
            async for chunk in resp.aiter_bytes():
                buf.append(chunk)
                yield chunk
        finally:
            try:
                await resp.aclose()
                await client.aclose()
            except Exception:
                pass
        # 流式输出护栏（后置记录）
        try:
            txt = b"".join(buf).decode("utf-8", "ignore")
            for line in txt.splitlines():
                if line.startswith("data: ") and line[6:] != "[DONE]":
                    try:
                        d = _json.loads(line[6:])
                        c = d.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if c:
                            _guard_log(c)
                    except Exception:
                        pass
        except Exception:
            pass

    return StreamingResponse(stream_gen(), status_code=resp.status_code,
                             media_type=resp.headers.get("content-type", "application/json"))


# ---------- vLLM PD 两阶段路由（官方 disaggregated prefill 协议） ----------

_pd_rr_counter: dict[tuple, int] = {}
_pd_rr_lock = threading.Lock()


def _pd_next(dep_id: int, role: str, n: int) -> int:
    """PD P/D 池轮询选择（部署×角色独立计数）"""
    with _pd_rr_lock:
        key = (dep_id, role)
        i = _pd_rr_counter.get(key, 0) % max(1, n)
        _pd_rr_counter[key] = _pd_rr_counter.get(key, 0) + 1
    return i


async def _pd_two_stage(dep: Deployment, rest: str, payload: dict,
                        request: Request, db, project_id: int, deploy_name: str):
    """vLLM PD 两阶段路由（vLLM ≥0.30 官方 toy_proxy 协议，v0.33.0 镜像验证）：

    1. P 实例（轮询）：max_tokens=1、stream=false、
       kv_transfer_params={do_remote_decode:true,...}；P 算完 prefill 持有 KV 块，
       响应体返回指向 KV 的 kv_transfer_params（min_tokens 类参数 P 不支持，暂移后补回）
    2. D 实例（轮询）：原始请求 + P 返回的 kv_transfer_params 转发；
       D 经 NIXL/Mooncake（RDMA 或 TCP）拉取 KV 后开始生成

    鉴权/护栏/模型名归一与协同模式一致（复用 openai_proxy 前置逻辑）。
    """
    import json as _json
    p_ips, d_ips = inf.pd_instance_ips(dep)
    p_ips = [ip for ip in p_ips if ip]
    d_ips = [ip for ip in d_ips if ip]
    if not p_ips or not d_ips:
        raise HTTPException(503, "PD 实例未就绪（P/D 实例 IP 不可解析）")
    # vLLM/SGLang 的 OpenAI 兼容路由在 /v1/ 下（对外路径 /v1/<部署名>/<rest>，转发引擎需补 /v1 前缀）
    api = "/v1/" + rest
    fwd_headers = {k: v for k, v in request.headers.items()
                   if k.lower() in ("content-type", "authorization", "x-api-key")}
    client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
    # ---- 阶段一：prefill（max_tokens=1 只算 prefill 不生成）----
    p_body = dict(payload) if isinstance(payload, dict) else {}
    p_body["stream"] = False
    p_body["max_tokens"] = 1
    if "max_completion_tokens" in p_body:
        p_body["max_completion_tokens"] = 1
    p_body.pop("stream_options", None)
    min_tokens = p_body.pop("min_tokens", None)
    min_completion_tokens = p_body.pop("min_completion_tokens", None)
    p_body["kv_transfer_params"] = {
        "do_remote_decode": True, "do_remote_prefill": False,
        "remote_engine_id": None, "remote_block_ids": None,
        "remote_host": None, "remote_port": None,
    }
    kvt: dict = {}
    p_err = ""
    for _ in range(len(p_ips)):
        p_ip = p_ips[_pd_next(dep.id, "p", len(p_ips))]
        try:
            r = await client.post(f"http://{p_ip}:8000{api}",
                                  content=_json.dumps(p_body).encode(), headers=fwd_headers)
            if r.status_code >= 400:
                p_err = f"prefill {p_ip} HTTP {r.status_code}: {r.text[:300]}"
                continue
            kvt = (r.json() or {}).get("kv_transfer_params") or {}
            if kvt:
                p_err = ""
                break
            p_err = f"prefill {p_ip} 未返回 kv_transfer_params"
        except Exception as e:
            p_err = f"prefill {p_ip} 请求失败: {e}"
    if p_err:
        await client.aclose()
        raise HTTPException(502, f"PD 分离 prefill 阶段失败: {p_err}")
    # ---- 阶段二：decode（原始请求 + KV 参数，D 拉取 KV 后生成）----
    d_body = dict(payload) if isinstance(payload, dict) else {}
    if min_tokens is not None:
        d_body["min_tokens"] = min_tokens
    if min_completion_tokens is not None:
        d_body["min_completion_tokens"] = min_completion_tokens
    d_body["kv_transfer_params"] = kvt
    d_ip = d_ips[_pd_next(dep.id, "d", len(d_ips))]
    try:
        areq = client.build_request("POST", f"http://{d_ip}:8000{api}",
                                    content=_json.dumps(d_body).encode(), headers=fwd_headers)
        resp = await client.send(areq, stream=True)
    except Exception as e:
        await client.aclose()
        raise HTTPException(502, f"PD 分离 decode 转发失败: {e}")
    return await _finish_forward(resp, client, db, project_id, deploy_name, rest, payload)


# ---------- 内部目标代理（网关端口池 31xx 承载，higress_sync 驱动） ----------
# 独立 ASGI app：不挂主路由（审计中间件/登录保护不作用于数据面），
# 每部署一个端口由 port_pool 起 uvicorn 线程服务；仅接受网关经 headerControl
# 注入内部令牌的请求（docker 网络旁路直调无令牌 → 401）。

from fastapi import FastAPI  # noqa: E402

target_app = FastAPI(title="VerveDeck 内部目标代理", docs_url=None, openapi_url=None)


def _target_db():
    """与 get_db 同语义的会话依赖（yield 依赖在响应完整结束后清理，流式安全）"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


@target_app.get("/health")
def _target_health():
    return {"status": "ok"}


@target_app.api_route("/v1/{deploy_name}/{rest:path}", methods=["POST", "GET"])
async def target_proxy(deploy_name: str, rest: str, request: Request,
                       db: Session = Depends(_target_db)):
    """网关加权端点入口：验内部令牌 → 代理核心（认证+护栏+PD 转发，与主端口一致）"""
    if request.headers.get("x-vervedeck-internal") != settings.target_proxy_token:
        raise HTTPException(401, "内部令牌校验失败（仅接受网关转发）")
    dep = db.query(Deployment).filter_by(name=deploy_name).first()
    if not dep:
        raise HTTPException(404, "部署不存在")
    return await _proxy_core(dep, rest, request, db)

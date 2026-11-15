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

# 启动线程令牌：每次 start/stop 换发新 token，作废旧后台线程，
# 防止旧线程 wait_ready 超时后回写 failed/running 覆盖新状态（竞态）
_launch_tokens: dict[int, str] = {}
_launch_lock = threading.Lock()


def _issue_launch_token(did: int) -> str:
    import uuid
    tok = uuid.uuid4().hex
    with _launch_lock:
        _launch_tokens[did] = tok
    return tok


def _launch_stale(did: int, tok: str) -> bool:
    with _launch_lock:
        return _launch_tokens.get(did) != tok


def _check(db, pid, user):
    p = db.get(Project, pid)
    if not p or not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")


def _resolve_model_path(asset: ModelAsset, db) -> str:
    if asset.type == "lora" and asset.parent_asset_id:
        base = db.get(ModelAsset, asset.parent_asset_id)
        if base:
            return base.path
    return asset.path


def _wait_engine_image(dep: Deployment, db2) -> str:
    """引擎镜像按需下载（懒加载）：本地缺失自动后台拉取并等待就绪。

    返回 "ready" 继续建容器；拉取失败/超时回写 failed 并返回 "stop"。
    """
    img = upgrade_service.current_image("vllm") if dep.engine == "vllm" \
        else upgrade_service.current_image("sglang")
    deadline = time.time() + 3600
    while True:
        ist = image_ops.ensure_image(img)
        if ist == "ready":
            return "ready"
        if ist == "failed":
            dep.status = "failed"
            dep.error = f"引擎镜像下载失败: {image_ops.image_status(img).get('error', '')}"
            db2.commit()
            return "stop"
        dep.status = "creating"
        msg = f"引擎镜像 {img} 下载中，完成后自动创建容器…"
        if dep.error != msg:
            dep.error = msg
            db2.commit()
        if time.time() > deadline:
            dep.status = "failed"
            dep.error = "引擎镜像下载超时（>1h），请在镜像管理页检查网络后重试"
            db2.commit()
            return "stop"
        time.sleep(10)


def _brief(d: Deployment, db: Session) -> dict:
    asset = db.get(ModelAsset, d.model_asset_id)
    return {
        "id": d.id, "project_id": d.project_id, "name": d.name,
        "model": asset.name if asset else "", "model_asset_id": d.model_asset_id,
        "model_type": asset.type if asset else "",
        "engine": d.engine, "quantization": d.quantization,
        "tensor_parallel": d.tensor_parallel, "max_model_len": d.max_model_len,
        "gpu_mem_util": d.gpu_mem_util, "enable_lora": d.enable_lora,
        "lora_adapters": d.lora_adapters or [], "replicas": d.replicas,
        "endpoint": d.endpoint, "status": d.status, "error": d.error,
        "description": d.description or "",
        "created_at": str(d.created_at),
        "openai_base_url": f"/v1/{d.name}" if d.status == "running" else "",
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


@router.post("/api/inference/deployments")
def create_deploy(req: DeployReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check(db, req.project_id, user)
    if req.engine not in ("vllm", "sglang"):
        raise HTTPException(400, "engine 需为 vllm/sglang")
    if db.query(Deployment).filter_by(name=req.name).first():
        raise HTTPException(400, "部署名已存在")
    asset = db.get(ModelAsset, req.model_asset_id)
    if not asset or asset.status != "ready":
        raise HTTPException(400, "模型资产不存在或未就绪")
    d = Deployment(
        project_id=req.project_id, name=req.name, model_asset_id=asset.id,
        engine=req.engine, quantization=req.quantization, tensor_parallel=req.tensor_parallel,
        max_model_len=req.max_model_len, gpu_mem_util=req.gpu_mem_util,
        enable_lora=req.enable_lora, lora_adapters=req.lora_adapters,
        replicas=max(1, req.replicas), description=req.description,
        status="creating", created_by=user.id,
    )
    db.add(d)
    db.commit()
    db.refresh(d)

    launch_tok = _issue_launch_token(d.id)

    def _launch():
        db2 = SessionLocal()
        try:
            dep = db2.get(Deployment, d.id)
            path = _resolve_model_path(asset, db2)
            if _wait_engine_image(dep, db2) != "ready":
                return
            inf.launch(dep, path)
            ready = inf.wait_ready(dep, timeout=1200)
            if _launch_stale(d.id, launch_tok):
                return
            dep = db2.get(Deployment, d.id)
            if ready:
                dep.status = "running"
                dep.error = ""  # 清除历史错误，避免 DB 显示陈旧 error
            else:
                dep.status = "failed"
                dep.error = "模型加载超时，请查看日志"
            db2.commit()
        except Exception as e:
            if _launch_stale(d.id, launch_tok):
                return
            dep2 = db2.get(Deployment, d.id)
            dep2.status = "failed"
            dep2.error = str(e)[:4000]
            db2.commit()
        finally:
            db2.close()

    threading.Thread(target=_launch, daemon=True).start()
    return _brief(d, db)


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
    asset = db.get(ModelAsset, d.model_asset_id)
    launch_tok = _issue_launch_token(d.id)

    def _launch():
        db2 = SessionLocal()
        try:
            dep = db2.get(Deployment, did)
            path = _resolve_model_path(asset, db2)
            if _wait_engine_image(dep, db2) != "ready":
                return
            inf.launch(dep, path)
            ready = inf.wait_ready(dep, timeout=1200)
            if _launch_stale(did, launch_tok):
                return  # 已被新的 start/stop 取代，不回写状态
            dep = db2.get(Deployment, did)
            dep.status = "running" if ready else "failed"
            dep.error = "" if ready else "模型加载超时"
            db2.commit()
        except Exception as e:
            if _launch_stale(did, launch_tok):
                return
            dep2 = db2.get(Deployment, did)
            dep2.status = "failed"
            dep2.error = str(e)[:4000]
            db2.commit()
        finally:
            db2.close()

    threading.Thread(target=_launch, daemon=True).start()
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


@router.post("/api/inference/deployments/{did}/scale")
def scale_deploy(did: int, req: dict, db=Depends(get_db), user=Depends(get_current_user)):
    """弹性扩缩容：调整副本数（需空闲 GPU）"""
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    from ..models import GpuNode, TrainingJob
    from .. import scheduler
    node = db.query(GpuNode).filter_by(name=settings.node_name).first()
    used = scheduler._gpu_in_use(db)
    want = max(1, int(req.get("replicas", d.replicas)))
    need = want * (d.tensor_parallel or 1)
    have = d.replicas * (d.tensor_parallel or 1)
    if node and need > (node.gpu_count or 0) - used + have:
        raise HTTPException(400, f"GPU 不足：需要 {need} 卡，可用 {max(0, (node.gpu_count or 0) - used + have)}")
    asset = db.get(ModelAsset, d.model_asset_id)
    path = _resolve_model_path(asset, db)
    # 先扩后缩
    if want > d.replicas:
        for i in range(d.replicas, want):
            inf.start_replica(d, i, path)
    else:
        for i in range(d.replicas - 1, want - 1, -1):
            tc.remove_container(inf.deployment_container_name(d, i), force=True)
    d.replicas = want
    d.container_ids = [inf.deployment_container_name(d, i) for i in range(want)]
    db.commit()
    # 扩缩容后容器 IP 变化，刷新 Prometheus file_sd target
    tgs = []
    for i in range(want):
        ip = tc.container_ip(inf.deployment_container_name(d, i))
        if ip:
            tgs.append(f"{ip}:8000")
    if tgs:
        prom_service.write_infer_sd(d.id, d.engine, inf.deployment_container_name(d, 0), tgs)
    return _brief(d, db)


@router.get("/api/inference/deployments/{did}/logs")
def deploy_logs(did: int, tail: int = 200, replica: int = 0, db=Depends(get_db), user=Depends(get_current_user)):
    d = db.get(Deployment, did)
    if not d:
        raise HTTPException(404, "部署不存在")
    _check(db, d.project_id, user)
    import os
    p = os.path.join(settings.data_dir, "infer", str(did), f"run-r{replica}.log")
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
    dep = db.query(Deployment).filter_by(name=deploy_name).first()
    if not dep:
        raise HTTPException(404, "部署不存在")
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
    # 输入护栏
    try:
        import json as _json
        payload = _json.loads(body) if body else {}
        msgs = payload.get("messages", [])
        in_text = " ".join(m.get("content", "") for m in msgs if isinstance(m, dict))
        hit = _check_guard(db, project_id, in_text, "input")
        if hit:
            return JSONResponse(status_code=451, content={"error": {"message": f"内容安全护栏拦截: {hit}"}})
    except Exception:
        pass

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

"""操作审计：中间件捕获写操作，记录用户/IP/路径/状态码/动作码

动作码（action）：(method, path) → 稳定机器码，前端按码做 i18n 可读化
（如 training_create → 「提交训练任务」），同时支持按动作筛选。
"""
import re
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .database import SessionLocal
from .models import AuditLog
from .iputil import client_ip

MUTATING = {"POST", "PUT", "DELETE", "PATCH"}
SKIP_PREFIXES = ("/api/dashboard", "/v1/", "/health")
# 安全事件状态码：未授权/拒绝访问无论读/写方法都必须留痕（安全治理审计要求）
SECURITY_EVENTS = (401, 403)

_RE_ADMIN_USER = re.compile(r"^/api/admin/users/\d+$")
_RE_ADMIN_GUARD = re.compile(r"^/api/admin/guard-rules/\d+$")
_RE_DATASET = re.compile(r"^/api/datasets/\d+$")
_RE_MODEL = re.compile(r"^/api/models/\d+$")
_RE_JOB = re.compile(r"^/api/training/jobs/\d+$")
_RE_EVAL = re.compile(r"^/api/evals/\d+$")
_RE_QUANT = re.compile(r"^/api/quantize/\d+$")
_RE_DEPLOY = re.compile(r"^/api/inference/deployments/\d+$")
_RE_APIKEY = re.compile(r"api-keys/\d+$")


def match_action(method: str, path: str, status_code: int) -> str:
    """(method, path, status) → 动作码。401/403 恒为 security（安全事件优先）。"""
    if status_code in SECURITY_EVENTS:
        return "security"
    m, p = method.upper(), path.rstrip("/")
    # 认证
    if p == "/api/auth/login":
        return "login"
    if p == "/api/auth/password":
        return "change_password"
    # 系统管理
    if p.startswith("/api/admin/"):
        if p == "/api/admin/users" and m == "POST":
            return "user_create"
        if _RE_ADMIN_USER.match(p):
            return "user_update"
        if p == "/api/admin/tenants" and m == "POST":
            return "tenant_create"
        if p == "/api/admin/projects" and m == "POST":
            return "project_create"
        if "/members" in p:
            return "project_member"
        if p.endswith("/api-keys") and m == "POST":
            return "apikey_create"
        if _RE_APIKEY.search(p):
            return "apikey_revoke"
        if p == "/api/admin/guard-rules" and m == "POST":
            return "guard_create"
        if _RE_ADMIN_GUARD.match(p):
            return "guard_delete"
        return "admin_other"
    # 数据集
    if p.startswith("/api/datasets"):
        if p == "/api/datasets" and m == "POST":
            return "dataset_create"
        if p == "/api/datasets/parse":
            return "dataset_parse"
        if p == "/api/datasets/clean":
            return "dataset_clean"
        if p == "/api/datasets/synth":
            return "dataset_synth"
        if p == "/api/datasets/import-s3":
            return "dataset_import_s3"
        if p.endswith("/versions/upload"):
            return "dataset_upload"
        if p.endswith("/commit"):
            return "dataset_commit"
        if p.endswith("/annotate"):
            return "dataset_annotate"
        if _RE_DATASET.match(p):
            return "dataset_delete"
        return "dataset_other"
    # 模型
    if p.startswith("/api/models"):
        if p == "/api/models/base":
            return "model_register_base"
        if p == "/api/models/upload":
            return "model_upload"
        if p == "/api/models" and m == "POST":
            return "model_register_artifact"
        if p.endswith("/meta"):
            return "model_update_meta"
        if _RE_MODEL.match(p):
            return "model_delete"
        return "model_other"
    # 训练
    if p.startswith("/api/training/"):
        if p == "/api/training/jobs" and m == "POST":
            return "training_create"
        if p.endswith("/cancel"):
            return "training_cancel"
        if p.endswith("/resume"):
            return "training_resume"
        if _RE_JOB.match(p):
            return "training_delete"
        return "training_other"
    # 评测
    if p.startswith("/api/evals"):
        if p == "/api/evals" and m == "POST":
            return "eval_create"
        if p == "/api/evals/compare":
            return "eval_compare"
        if _RE_EVAL.match(p):
            return "eval_delete"
        return "eval_other"
    # 模型压缩
    if p.startswith("/api/quantize"):
        if p == "/api/quantize" and m == "POST":
            return "quant_create"
        if _RE_QUANT.match(p):
            return "quant_delete"
        return "quant_other"
    # 推理部署
    if p.startswith("/api/inference/deployments"):
        if p == "/api/inference/deployments" and m == "POST":
            return "deploy_create"
        if p.endswith("/start"):
            return "deploy_start"
        if p.endswith("/stop"):
            return "deploy_stop"
        if p.endswith("/scale"):
            return "deploy_scale"
        if p.endswith("/adapters") and m == "POST":
            return "deploy_lora"
        if p.endswith("/adapters"):
            return "deploy_lora_remove"
        if _RE_DEPLOY.match(p):
            return "deploy_delete"
        return "deploy_other"
    # 集群调度
    if p.startswith("/api/cluster/queue"):
        return "cluster_priority"
    return "other"


def _should_record(method: str, path: str, status_code: int) -> bool:
    if not path.startswith("/api/"):
        return False
    if any(path.startswith(p) for p in SKIP_PREFIXES):
        return False
    if method in MUTATING:
        return True
    return status_code in SECURITY_EVENTS


class AuditMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        try:
            path = request.url.path
            if _should_record(request.method, path, response.status_code):
                user = getattr(request.state, "user", None)
                db = SessionLocal()
                try:
                    db.add(AuditLog(
                        ts=int(time.time()),
                        user_id=user.id if user else None,
                        username=user.username if user else "",
                        method=request.method, path=path,
                        ip=client_ip(request),
                        status_code=response.status_code,
                        detail=(request.query_params.get("id") or "")[:200],
                        action=match_action(request.method, path, response.status_code),
                    ))
                    db.commit()
                finally:
                    db.close()
        except Exception:
            pass
        return response

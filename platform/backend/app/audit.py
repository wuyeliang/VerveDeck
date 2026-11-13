"""操作审计：中间件捕获写操作，记录用户/IP/路径/状态码"""
import time

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

from .database import SessionLocal
from .models import AuditLog

MUTATING = {"POST", "PUT", "DELETE", "PATCH"}
SKIP_PREFIXES = ("/api/dashboard", "/v1/", "/health")
# 安全事件状态码：未授权/拒绝访问无论读/写方法都必须留痕（安全治理审计要求）
SECURITY_EVENTS = (401, 403)


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
                        ip=request.client.host if request.client else "",
                        status_code=response.status_code,
                        detail=(request.query_params.get("id") or "")[:200],
                    ))
                    db.commit()
                finally:
                    db.close()
        except Exception:
            pass
        return response

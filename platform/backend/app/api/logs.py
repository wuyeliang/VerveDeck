"""日志查看：平台容器 + 任务容器的 docker logs 读取（仅超管）"""
from fastapi import APIRouter, Depends, HTTPException

from ..security import require_role

router = APIRouter(prefix="/api/logs", tags=["logs"])

# 平台常驻容器（fintune-{service}-1）
PLATFORM_SERVICES = ["backend", "web", "gpu-agent", "argilla", "opensearch",
                     "mineru", "rustfs", "postgres", "redis"]


def _docker():
    from ..services import taskcontainer
    return taskcontainer.docker_client()


@router.get("/services")
def services(user=Depends(require_role("super_admin"))):
    """可查日志的服务列表：平台容器 + 任务容器（ft-*）"""
    try:
        c = _docker()
        out = []
        for s in PLATFORM_SERVICES:
            name = f"fintune-{s}-1"
            try:
                ct = c.containers.get(name)
                out.append({"name": s, "container": name,
                            "running": ct.status == "running", "kind": "platform"})
            except Exception:
                out.append({"name": s, "container": name, "running": False, "kind": "platform"})
        for ct in c.containers.list(all=True):
            n = ct.name or ""
            if n.startswith("ft-"):  # 任务容器：ft-train-N / ft-infer-N / ...
                out.append({"name": n, "container": n,
                            "running": ct.status == "running", "kind": "task"})
    except Exception as e:
        raise HTTPException(500, f"Docker 不可用: {str(e)[:200]}")
    return out


@router.get("")
def read_logs(service: str, tail: int = 200, user=Depends(require_role("super_admin"))):
    """读取容器日志（最近 tail 行，带时间戳；级别/关键词过滤在前端做）"""
    tail = max(10, min(int(tail), 5000))
    name = service
    if service in PLATFORM_SERVICES:
        name = f"fintune-{service}-1"
    try:
        c = _docker()
        ct = c.containers.get(name)
        raw = ct.logs(tail=tail, timestamps=True)
        lines = raw.decode("utf-8", "replace").splitlines()
        return {"service": service, "container": name,
                "running": ct.status == "running", "lines": lines}
    except LookupError:
        raise HTTPException(404, f"容器不存在: {name}")
    except Exception as e:
        raise HTTPException(500, f"读取日志失败: {str(e)[:200]}")

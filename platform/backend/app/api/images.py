"""镜像管理：平台引擎镜像清单 + 版本探测 + 拉取/重建 + 自定义镜像（国产芯片等）

- 官方开源镜像（vllm/vllm-openai、lmsysorg/sglang、hiyouga/llamafactory、
  nvidia/dcgm）：refresh = docker pull
- 自构镜像（fintune/*）：refresh = 以 compose 挂载的 /repo（宿主源码树，只读）
  为上下文通过 docker socket 流式构建
- 版本探测：一次性容器内 import 关键包读版本号，结果缓存 5 分钟
- 拉取/重建均为后台异步（image_ops），页面轮询 pull_status 展示进度状态
"""
import threading
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..security import require_role
from ..models import CustomImage, User
from ..services import image_ops
from ..services import upgrade_service

router = APIRouter(prefix="/api/images", tags=["images"])

PROBE_TTL = 300

# key -> (镜像引用来源, 组件, 说明, 版本探测命令)
# 注意：官方开源镜像（vllm/sglang）内只有 python3，无 python 别名，探测命令一律用 python3
PLATFORM = [
    # 镜像引用统一走运行时 pin（集群升级后此处自动反映新 tag）
    ("vllm", lambda: upgrade_service.current_image("vllm"), "vLLM 推理引擎", "OpenAI 兼容 API / LoRA 热加载",
     'python3 -c "import vllm,torch;print(vllm.__version__+\' | torch \'+torch.__version__+\' | CUDA \'+str(torch.version.cuda))"'),
    ("sglang", lambda: upgrade_service.current_image("sglang"), "SGLang 推理引擎", "高吞吐 RadixAttention",
     'python3 -c "import sglang,torch;print(sglang.__version__+\' | torch \'+torch.__version__+\' | CUDA \'+str(torch.version.cuda))"'),
    ("llamafactory", lambda: upgrade_service.current_image("llamafactory"), "LLaMA-Factory 训练引擎", "SFT/DPO/RLHF/导出",
     'python3 -c "import importlib.metadata as m,torch;print(m.version(\'llamafactory\')+\' | torch \'+torch.__version__+\' | CUDA \'+str(torch.version.cuda))"'),
    ("gpu-agent", lambda: upgrade_service.current_image("gpu_agent"), "GPU 监控 Agent", "DCGM 采集（开源 nvidia/dcgm 镜像 + collect.sh 挂载）",
     # dcgmi 非 TTY 下不输出版本，改查 dpkg 包版本（内层单引号防 shell 展开 ${Version}）；
     # DCGM 4.x 包拆分为 nv-hostengine/dcgmi（3.x 为 datacenter-gpu-manager），双查兜底
     "dpkg-query -W -f 'DCGM ${Version}' nv-hostengine 2>/dev/null || dpkg-query -W -f 'DCGM ${Version}' datacenter-gpu-manager 2>/dev/null"),
    # 官方开源 ModelScope 镜像（内置 evalscope）；查包元数据即可（importlib.metadata
    # 不触发 torch 重导入，探测秒级返回；勿 import evalscope 本体，首启可达数十秒）
    ("evalscope", lambda: upgrade_service.current_image("evalscope"), "evalscope 评测引擎",
     "规则匹配 / LLM-as-a-Judge / 榜单评测（ModelScope 官方镜像，runner.py 运行时投递）",
     'python3 -c "import importlib.metadata as m;print(m.version(\'evalscope\'))"'),
]

_probe_cache: dict[str, tuple[float, str]] = {}


def _admin(user: User = Depends(require_role("super_admin"))):
    return user


def _docker():
    # 复用全局单例 client（每次 from_env 会新建 API 会话，列表页一次请求多次调用会叠加开销）
    from ..services.taskcontainer import docker_client
    return docker_client()


def _image_info(image: str) -> dict:
    out = {"exists": False, "size": 0}
    try:
        imgs = _docker().images.list(image)
        if imgs:
            out["exists"] = True
            out["size"] = int(imgs[0].short_id and imgs[0].attrs.get("Size") or 0)
    except Exception:
        pass
    return out


# 探测结果双缓存：
# - _probe_cache：TTL 缓存（PROBE_TTL 内直接命中，零开销）
# - _probe_stale：无 TTL 兜底值（过期/首探未完成时立即展示旧值，避免页面空转）
# 探测本体（起容器 import 引擎包，vllm 可达数十秒）只跑在后台线程，
# 列表/状态接口永不阻塞——否则 backend 重启后首开页面要等 1-3 分钟才出数据。
_probe_stale: dict[str, str] = {}
_probe_busy: set[str] = set()
_probe_lock = threading.Lock()


def _probe_sync(key: str, image: str, cmd: str) -> str:
    """执行一次真实探测并写双缓存（供后台线程/手动刷新调用，可能阻塞数十秒）"""
    now = time.time()
    try:
        d = _docker()
        if not d.images.list(image):
            res = "镜像未拉取"
        else:
            # detach=True 才返回容器对象（非 detach 时 run 直接返回 bytes 输出流，
            # 且非零退出会抛 ContainerError）；wait 结束后取日志，finally 保证清理不泄漏
            ctr = d.containers.run(image, entrypoint=["sh", "-c", cmd],
                                   detach=True, stdout=True, stderr=True, remove=False)
            try:
                try:
                    ctr.wait(timeout=90)
                except Exception:
                    pass
                out = ctr.logs(stdout=True, stderr=False).decode("utf-8", "ignore").strip()
            finally:
                try:
                    ctr.remove(force=True)
                except Exception:
                    pass
            res = out.split("\n")[-1].strip() if out else "探测无输出"
    except Exception as e:
        res = f"探测失败: {str(e)[:160]}"
    _probe_cache[key] = (now, res)
    _probe_stale[key] = res
    return res


def _probe_bg(key: str, image: str, cmd: str) -> None:
    try:
        _probe_sync(key, image, cmd)
    finally:
        with _probe_lock:
            _probe_busy.discard(key)


def _probe(key: str, image: str, cmd: str, force: bool = False) -> str:
    now = time.time()
    hit = _probe_cache.get(key)
    if not force and hit and now - hit[0] < PROBE_TTL:
        return hit[1]
    if force:
        # 用户点「探测」按钮：显式刷新，同步等待新结果
        return _probe_sync(key, image, cmd)
    # 缓存过期或首探未完成：立即返回（旧值兜底 / 探测中占位），后台刷新
    with _probe_lock:
        if key not in _probe_busy:
            _probe_busy.add(key)
            threading.Thread(target=_probe_bg, args=(key, image, cmd),
                             name=f"probe-{key}", daemon=True).start()
    return _probe_stale.get(key) or "探测中…"


@router.get("")
def list_images(user=Depends(_admin)):
    items = []
    for key, img_fn, comp, desc, cmd in PLATFORM:
        image = img_fn()
        info = _image_info(image)
        # 自构镜像统一 fintune/* 前缀；其余（Docker Hub 或 ACR 完整地址）均为官方开源
        source = "selfbuilt" if image.startswith("fintune/") else "official"
        items.append({
            "key": key, "image": image, "component": comp, "desc": desc,
            "source": source, "exists": info["exists"], "size": info["size"],
            "version": _probe(key, image, cmd),
            "pull_status": image_ops.image_status(image),
        })
    customs = [
        {"key": f"custom-{c.id}", "image": c.name, "component": "自定义镜像",
         "desc": c.note, "source": "custom",
         "exists": c.status == "present", "size": c.size or 0, "version": c.status}
        for c in _docker_list_customs()
    ]
    return items + customs


def _customs():
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        return db.query(CustomImage).order_by(CustomImage.id.asc()).all()
    finally:
        db.close()


def _docker_list_customs():
    return _customs()


class CustomReq(BaseModel):
    name: str
    note: str = ""


@router.get("/custom")
def list_custom(user=Depends(_admin)):
    return [_custom_row(c) for c in _customs()]


def _custom_row(c: CustomImage) -> dict:
    info = _image_info(c.name)
    return {"id": c.id, "name": c.name, "note": c.note,
            "status": "present" if info["exists"] else "missing",
            "size": info["size"], "created_at": str(c.created_at)}


@router.post("/custom")
def add_custom(req: CustomReq, user=Depends(_admin)):
    name = (req.name or "").strip()
    if ":" not in name:
        raise HTTPException(400, "镜像需为 name:tag 形式（如 ascend/vllm-ascend:latest）")
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        if db.query(CustomImage).filter(CustomImage.name == name).first():
            raise HTTPException(400, "已登记该镜像")
        c = CustomImage(name=name, note=req.note[:255])
        db.add(c)
        db.commit()
        db.refresh(c)
        return _custom_row(c)
    except HTTPException:
        raise
    finally:
        db.close()


@router.delete("/custom/{cid}")
def delete_custom(cid: int, user=Depends(_admin)):
    from ..database import SessionLocal
    db = SessionLocal()
    try:
        c = db.get(CustomImage, cid)
        if not c:
            raise HTTPException(404, "不存在")
        db.delete(c)
        db.commit()
        return {"ok": True}
    except HTTPException:
        raise
    finally:
        db.close()


class BatchDeleteReq(BaseModel):
    ids: list[int]


@router.post("/custom/batch-delete")
def batch_delete_custom(req: BatchDeleteReq, user=Depends(_admin)):
    """批量移除自定义镜像登记：逐个执行单删逻辑，返回部分结果"""
    deleted: list[int] = []
    failed: list[dict] = []
    for cid in req.ids:
        try:
            delete_custom(cid, user=user)
            deleted.append(cid)
        except HTTPException as e:
            failed.append({"id": cid, "error": str(e.detail)})
    return {"deleted": deleted, "failed": failed}


@router.post("/{key}/probe")
def probe(key: str, user=Depends(_admin)):
    """强制刷新版本探测（缓存失效）"""
    for k, img_fn, comp, desc, cmd in PLATFORM:
        if k == key:
            return {"ok": True, "version": _probe(key, img_fn(), cmd, force=True)}
    raise HTTPException(404, "未知镜像组件")


@router.post("/{key}/refresh")
def refresh(key: str, force: bool = False, user=Depends(_admin)):
    """后台拉取/重建（异步，立即返回）：
    - 官方镜像 docker pull；自构镜像以 /repo 上下文流式 build
    - force=true 且镜像已存在时强制重新拉取/重建（版本升级用）
    进度状态通过 GET /api/images 的 pull_status 轮询。
    """
    for k, img_fn, comp, desc, cmd in PLATFORM:
        if k != key:
            continue
        image = img_fn()
        st = image_ops.ensure_image(image, force=force)
        if st == "failed":
            raise HTTPException(502, f"操作失败: {image_ops.image_status(image).get('error', '')}")
        # 拉取/重建后旧版本值已失效：双缓存同清，下次列表触发后台重探（显示「探测中…」）
        _probe_cache.pop(key, None)
        _probe_stale.pop(key, None)
        return {"ok": True, "action": st, "image": image}
    raise HTTPException(404, "未知镜像组件")

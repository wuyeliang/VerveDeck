"""推理后端注册表：builtin 种子对齐 + 自定义后端解析 + 启动命令模板渲染

builtin（vllm/sglang）：引擎逻辑内置于代码（inference_service.build_cmd），
注册表条目承担展示与参数默认值；生效镜像以镜像 pin 系统为准
（upgrade_service.current_image，镜像页运行时升级自动生效）。
custom（名须以 -custom 结尾）：版本决定镜像 + 启动命令模板（占位符渲染），
单机闭环、不支持 PD 分离（API 层拦截，build_cmd 防御兜底）。

占位符集（run_command 模板）：
    {{model_path}}  模型权重路径
    {{port}}        服务端口（固定 8000，与平台代理/探针口径一致）
    {{model_name}}  对外服务名（served model name）
    {{worker_ip}}   本容器 IP（容器创建后解析；单实例即自身）
    {{gpu_count}}   本实例 GPU 数（= tensor_parallel）
    {{gpu_ids}}     容器内 GPU 编号列表（0,1,…,N-1，容器内重编号口径）
    {{VAR_NAME}}    任意 default_env / version.env 的键；
                    kv_cache_ext 有值时另有 {{cpu_offload_gb}}/{{hicache_size}}
未知占位符保留原样并告警（不静默吞掉用户配置）。
"""
import logging
import re

from ..config import settings
from ..models import InferenceBackend, InferenceBackendVersion
from . import upgrade_service

log = logging.getLogger("backend-registry")

BUILTIN_ENGINE_NAMES = ("vllm", "sglang")
CUSTOM_SUFFIX = "-custom"

# 内置后端参数默认值（前端表单渲染用；与 build_cmd 参数面同口径）
_VLLM_DEFAULT_PARAMS = {
    "max_model_len": 8192, "gpu_mem_util": 0.85, "quantization": "none",
    "tensor_parallel": 1, "max_num_seqs": None, "dtype": "auto",
    "enforce_eager": False, "enable_prefix_caching": False,
    "enable_chunked_prefill": False, "api_key": "", "extra_args": "",
}
_SGLANG_DEFAULT_PARAMS = {
    "max_model_len": 8192, "mem_fraction_static": None, "quantization": "none",
    "tensor_parallel": 1, "disable_cuda_graph": False, "extra_args": "",
}


def is_custom_name(name: str | None) -> bool:
    """是否自定义后端名（非空且非 builtin 引擎名）"""
    name = (name or "").strip()
    return bool(name) and name not in BUILTIN_ENGINE_NAMES


def _tag_of(image: str) -> str:
    """镜像 tag（builtin 基线版本名）；末段无 tag 视为 latest"""
    last = image.rsplit("/", 1)[-1]
    return last.rsplit(":", 1)[-1] if ":" in last else "latest"


def seed_builtin_backends(db) -> int:
    """幂等补建 builtin 后端（vllm/sglang）与基线版本（compose 基准镜像）。
    仅缺失时创建，不覆盖已有（保留用户对 builtin 元数据的编辑）。返回新建条目数。"""
    seeds = [
        ("vllm", settings.images_vllm, "vLLM：PagedAttention + OpenAI 兼容 API", _VLLM_DEFAULT_PARAMS),
        ("sglang", settings.images_sglang, "SGLang：RadixAttention + OpenAI 兼容 API", _SGLANG_DEFAULT_PARAMS),
    ]
    created = 0
    for name, image, desc, params in seeds:
        b = db.query(InferenceBackend).filter_by(name=name).first()
        if not b:
            b = InferenceBackend(name=name, source="builtin", description=desc,
                                 health_check_path="/v1/models", default_params=dict(params))
            db.add(b)
            db.flush()
            created += 1
        if not db.query(InferenceBackendVersion).filter_by(
                backend_id=b.id, version=_tag_of(image)).first():
            db.add(InferenceBackendVersion(backend_id=b.id, version=_tag_of(image),
                                           image=image, is_default=True))
            created += 1
    return created


def validate_choice(db, name: str | None, version: str | None, pd_mode: str = "off"):
    """部署后端选型校验（API 创建/编辑共用）；失败抛 ValueError（API 转 400）。

    空 / builtin 名 → (None, None)（builtin 镜像走 pin 系统，版本表仅展示基线）；
    custom → 须存在于注册表，版本（显式或默认）须存在，不支持 PD 分离。
    返回归一化后的 (backend_name, backend_version)。"""
    name = (name or "").strip() or None
    version = (version or "").strip() or None
    if not name or name in BUILTIN_ENGINE_NAMES:
        return None, None
    b = db.query(InferenceBackend).filter_by(name=name).first()
    if not b:
        raise ValueError(f"推理后端 {name} 不存在（请先在「推理后端」页创建）")
    if b.source == "builtin":  # 显式选 builtin 等价于不选（防御口径）
        return None, None
    if (pd_mode or "off") == "pd":
        raise ValueError("自定义后端不支持 PD 分离")
    if version:
        v = db.query(InferenceBackendVersion).filter_by(backend_id=b.id, version=version).first()
        if not v:
            raise ValueError(f"推理后端 {name} 无版本 {version}")
    elif not db.query(InferenceBackendVersion).filter_by(backend_id=b.id, is_default=True).first():
        raise ValueError(f"推理后端 {name} 无默认版本，请显式指定 backend_version")
    return name, version


def backend_context(name: str, version: str | None = None) -> dict:
    """解析自定义后端上下文 {name, image, run_command, env, health_check_path}。
    后端/版本缺失抛 RuntimeError（launch 线程记 failed，不静默回退 builtin）。
    自开短会话（调用点在容器创建线程/API，均无活动请求会话）。"""
    from ..database import SessionLocal
    name = (name or "").strip()
    db = SessionLocal()
    try:
        b = db.query(InferenceBackend).filter_by(name=name).first()
        if not b:
            raise RuntimeError(f"推理后端 {name} 不存在（请先在「推理后端」页创建）")
        if version:
            v = db.query(InferenceBackendVersion).filter_by(backend_id=b.id, version=version).first()
            if not v:
                raise RuntimeError(f"推理后端 {name} 无版本 {version}")
        else:
            v = db.query(InferenceBackendVersion).filter_by(backend_id=b.id, is_default=True).first()
            if not v:
                raise RuntimeError(f"推理后端 {name} 无默认版本")
        return {
            "name": b.name,
            "image": v.image,
            "run_command": v.run_command or "",
            "env": {str(k): str(val) for k, val in
                    {**(b.default_env or {}), **(v.env or {})}.items()},
            "health_check_path": (b.health_check_path or "/v1/models").strip() or "/v1/models",
        }
    finally:
        db.close()


_PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")


def render_run_command(template: str, ctx: dict) -> str:
    """占位符渲染；未知占位符保留原样并告警（避免静默吞掉用户配置）"""
    def _sub(m: re.Match) -> str:
        key = m.group(1)
        if key in ctx and ctx[key] is not None:
            return str(ctx[key])
        log.warning("run_command 未知占位符 %s，保留原样", m.group(0))
        return m.group(0)
    return _PLACEHOLDER.sub(_sub, template or "")


def custom_cmd(dep, model_path: str, role: str | None, idx: int,
               worker_ip: str, served: str) -> list[str]:
    """自定义后端启动命令（bash -lc 脚本串，与 vLLM 启动形态一致）。
    固定占位符优先级高于 env 同名键（防用户 env 覆盖模型路径等关键值）。"""
    name = (getattr(dep, "backend_name", "") or "").strip()
    if role is not None:
        raise RuntimeError("自定义后端不支持 PD 分离")
    ctx0 = backend_context(name, getattr(dep, "backend_version", None) or None)
    tp = max(1, getattr(dep, "tensor_parallel", None) or 1)
    kv = {str(k): str(val) for k, val in (getattr(dep, "kv_cache_ext", None) or {}).items()}
    ctx = {
        **ctx0["env"],
        **kv,
        "model_path": model_path,
        "port": "8000",
        "model_name": served,
        "worker_ip": worker_ip or "127.0.0.1",
        "gpu_count": str(tp),
        "gpu_ids": ",".join(str(i) for i in range(tp)),
    }
    rendered = render_run_command(ctx0["run_command"], ctx)
    if not rendered.strip():
        raise RuntimeError(f"自定义后端 {name} 的启动命令模板为空")
    return ["bash", "-lc", rendered + " 2>&1"]

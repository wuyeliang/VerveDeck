"""推理部署服务（vLLM / SGLang 引擎）

架构：每个部署 = 一个独立 pause 占位容器，exec 拉起 vllm serve / sglang.launch_server。
- OpenAI 兼容 API 通过 backend 反向代理 /v1/<deploy_name>/... 对外发布
- 多 LoRA：启动时 --enable-lora --lora-modules，运行期可热加载 (vLLM /v1/loras)
- 量化：--quantization awq/gptq/int8（按模型权重实际量化格式）
- 弹性扩缩容：replicas>1 时创建多副本容器，代理轮询
"""
import json
import os
import re
import shlex
import time
import logging
import threading

import requests

from ..config import settings
from ..models import Deployment, ModelAsset
from ..database import SessionLocal
from . import taskcontainer as tc
from . import upgrade_service
from . import backend_registry
from . import cache_service

log = logging.getLogger("inference")

_counter = 0
_counter_lock = threading.Lock()


def _next_rr(deployment_id: int, replicas: int) -> int:
    global _counter
    with _counter_lock:
        _counter += 1
        return _counter % replicas


def deployment_container_name(dep: Deployment, replica: int = 0) -> str:
    # 副本容器名必须是"稳定标识"，不能随 dep.replicas 变化而漂移：
    #   副本 0 恒为 ft-infer-{id}（与 scheduler._reconcile_deployments 的兜底口径一致），
    #   其余副本恒为 ft-infer-{id}-r{i}。
    # 旧实现 `dep.replicas <= 1` 使副本 0 的名字在 ft-infer-{id}（1 副本）与
    # ft-infer-{id}-r0（≥2 副本）之间切换：扩容瞬间旧容器被孤儿化、GPU 显存泄漏，
    # 调度器又按新名字重建 r0/r1，多容器抢占卡 → 模型加载超时 → 整个部署误判 failed。
    return f"ft-infer-{dep.id}" if replica == 0 else f"ft-infer-{dep.id}-r{replica}"


# ---------- PD 分离 / 多级多卡拓扑（#103） ----------

def _is_pd(dep: Deployment) -> bool:
    return (getattr(dep, "pd_mode", None) or "off") == "pd"


def _vllm_connector(dep: Deployment) -> str:
    """KV connector 归一：nixl（默认，NIXL/UCX，RDMA 或 TCP 均可）/ mooncake"""
    c = str(getattr(dep, "kv_connector", "") or "").strip().lower()
    return c if c in ("nixl", "mooncake") else "nixl"


def _sglang_backend(dep: Deployment) -> str:
    """SGLang PD 传输后端（与 vLLM kv_connector 同名口径：nixl/mooncake）"""
    return _vllm_connector(dep)


def pd_instance_name(dep: Deployment, role: str, idx: int) -> str:
    """PD 实例容器名：prefill→ft-infer-{id}-p{idx}，decode→ft-infer-{id}-d{idx}"""
    return f"ft-infer-{dep.id}-{'p' if role == 'prefill' else 'd'}{idx}"


def pd_router_name(dep: Deployment) -> str:
    """SGLang PD router 容器名（vLLM PD 路由内置于 backend 代理，无独立容器）"""
    return f"ft-infer-{dep.id}-rt"


def pd_side_port(dep: Deployment, role: str, idx: int) -> int:
    """vLLM NIXL side-channel 端口（同宿主每 worker 必须唯一；按部署 id 错开防冲突）"""
    off = (dep.id * 17) % 1000
    return 5600 + off + (0 if role == "prefill" else 250) + idx


def pd_bootstrap_port(dep: Deployment, idx: int) -> int:
    """SGLang prefill 侧 bootstrap 端口（router/decode 经它获取 KV 传输参数）"""
    return 8900 + (dep.id * 7) % 100 + idx


def deployment_gpu_need(dep: Deployment) -> int:
    """部署占用 GPU 总数（调度准入 / 扩缩容复检 / GPU 看板）。

    协同：replicas × tensor_parallel；PD：prefill 池 + decode 池
    （router 容器 CPU-only 不计卡；node_count 是放置分组维度，不乘算）"""
    if _is_pd(dep):
        return (max(1, dep.prefill_replicas or 1) * max(1, dep.prefill_tp or 1)
                + max(1, dep.decode_replicas or 1) * max(1, dep.decode_tp or 1))
    return (dep.tensor_parallel or 1) * max(dep.replicas, 1)


def pd_instance_ips(dep: Deployment) -> tuple[list[str], list[str]]:
    """(prefill 实例 ip 列表, decode 实例 ip 列表)——backend 两阶段代理按此寻址。
    container_ids 布局 [p0..pA, d0..dB, (rt 仅 SGLang)]；IP 每次实时解析
    （容器重建后 IP 变化），查不到返回空串由调用方过滤。"""
    n_p = max(1, dep.prefill_replicas or 1)
    n_d = max(1, dep.decode_replicas or 1)
    ids = list(dep.container_ids or [])
    p_ids = ids[:n_p]
    d_ids = ids[n_p:n_p + n_d]
    return ([tc.container_ip(c) for c in p_ids],
            [tc.container_ip(c) for c in d_ids])


def start_pd_instance(dep: Deployment, role: str, idx: int, model_path: str) -> str:
    """创建并启动一个 PD 角色实例（prefill/decode），返回容器 id。

    通信 env：
    - 容器级：基础 env + dep.nccl_env（用户 NCCL/UCX/NIXL 覆盖，
      如 NCCL_IB_HCA / NCCL_SOCKET_IFNAME / UCX_NET_DEVICES）
    - exec 级：VLLM_NIXL_SIDE_CHANNEL_PORT（每 worker 唯一）+ prefill 侧
      VLLM_NIXL_SIDE_CHANNEL_HOST=本容器 IP（decode 跨容器拉 KV 的握手地址）
    RDMA：宿主有 IB 时 taskcontainer 自动注入 uverbs/rdma_cm + ipc=host，
    NIXL/NCCL 自动走 RDMA；无 IB 时走容器桥（UCX TCP 传输），功能一致。
    """
    name = pd_instance_name(dep, role, idx)
    tc.remove_container(name, force=True)
    tp = dep.prefill_tp if role == "prefill" else dep.decode_tp
    cid = tc.create_container(
        name=name,
        image=upgrade_service.current_image("vllm") if dep.engine == "vllm"
        else upgrade_service.current_image("sglang"),  # PD 仅 builtin 引擎（自定义后端 API 层拦截）
        gpus=max(tp, 1),
        cpus=8, mem_gb=20,
        env={"VLLM_LOGGING_LEVEL": "warning",
             "VLLM_ALLOW_RUNTIME_LORA_UPDATING": "1",
             **(dep.nccl_env or {}),
             **cache_service.cache_env_for(dep)},
        labels={"fintune.deployment": str(dep.id), "fintune.type": "inference",
                "fintune.engine": dep.engine, "fintune.pd_role": role},
    )
    cmd = build_cmd(dep, model_path, role=role, idx=idx, worker_ip=tc.container_ip(cid))
    exec_env: dict[str, str] = {}
    if dep.engine == "vllm":
        exec_env["VLLM_NIXL_SIDE_CHANNEL_PORT"] = str(pd_side_port(dep, role, idx))
        if role == "prefill":
            ip = tc.container_ip(cid)
            if ip:
                exec_env["VLLM_NIXL_SIDE_CHANNEL_HOST"] = ip
    log_path = os.path.join(settings.data_dir, "infer", str(dep.id),
                            f"run-{role[0]}{idx}.log")
    tc.start_task(f"infer-{dep.id}-{role[0]}{idx}", cid, cmd, log_path, env=exec_env)
    return cid


def start_pd_router(dep: Deployment, model_path: str) -> str:
    """SGLang PD router 容器（镜像自带 sglang_router，CPU-only）。

    vLLM PD 不走此路：路由内置于 backend 代理（两阶段调用，见 api/inference.py），
    免额外镜像，鉴权/护栏/模型名归一与协同模式完全一致。
    依赖 container_ids 已按 [p0..pA, d0..dB] 布局（launch 先建实例后建 router）。
    """
    name = pd_router_name(dep)
    tc.remove_container(name, force=True)
    n_p = max(1, dep.prefill_replicas or 1)
    n_d = max(1, dep.decode_replicas or 1)
    ids = list(dep.container_ids or [])
    p_ips = [tc.container_ip(c) for c in ids[:n_p]]
    d_ips = [tc.container_ip(c) for c in ids[n_p:n_p + n_d]]
    args = ["python3", "-m", "sglang_router.launch_router",
            "--host", "0.0.0.0", "--port", "8000", "--pd-disaggregation"]
    for i, ip in enumerate(p_ips):
        if ip:
            args += ["--prefill", f"http://{ip}:8000", str(pd_bootstrap_port(dep, i))]
    for ip in d_ips:
        if ip:
            args += ["--decode", f"http://{ip}:8000"]
    cid = tc.create_container(
        name=name,
        image=upgrade_service.current_image("sglang"),
        gpus=0, cpus=4, mem_gb=4,
        env={**(dep.nccl_env or {})},
        labels={"fintune.deployment": str(dep.id), "fintune.type": "inference",
                "fintune.engine": dep.engine, "fintune.pd_role": "router"},
    )
    tc.start_task(f"infer-{dep.id}-rt", cid, args,
                  os.path.join(settings.data_dir, "infer", str(dep.id), "run-rt.log"))
    return cid


def _adv(dep: Deployment) -> dict:
    """高级参数（Deployment.advanced）；非 dict 视为空（留空=引擎默认，存量部署行为不变）"""
    a = getattr(dep, "advanced", None) or {}
    return a if isinstance(a, dict) else {}


def _served_name(dep: Deployment) -> str:
    """引擎实际 --served-model-name：高级参数覆盖，否则回退部署名（与 api/inference._served_name 同口径）"""
    return (_adv(dep).get("served_model_name") or "").strip() or dep.name


_DTYPE_CLI = {"auto": "auto", "bfloat16": "bfloat16", "bf16": "bfloat16",
              "float16": "float16", "fp16": "float16"}


def _dtype_cli(v) -> str:
    """前端 dtype(auto/bf16/fp16) → vLLM --dtype 取值；非法/留空返回 ''（不加该参数）"""
    return _DTYPE_CLI.get(str(v or "").strip().lower(), "")


def _truthy(v) -> bool:
    return bool(v) and str(v).lower() not in ("0", "false", "no", "off", "")


def _extra_argv(raw) -> list[str]:
    """extra_args 原始命令行 → argv token 列表（SGLang 走 argv；引号不配对退化为空格切分）"""
    raw = (raw or "").strip()
    if not raw:
        return []
    try:
        return shlex.split(raw)
    except ValueError:
        return raw.split()


def _kv_ext(dep: Deployment) -> dict:
    """部署级扩展 KV 缓存参数（非 dict 视为空）：
    vLLM {cpu_offload_gb} → --cpu-offload-gb；SGLang {hicache_size} → --hicache-size"""
    k = getattr(dep, "kv_cache_ext", None) or {}
    return k if isinstance(k, dict) else {}


def _backend_context(dep: Deployment) -> dict:
    """自定义后端上下文（image/run_command/env）；builtin 引擎返回空 dict（走既有路径）。
    后端/版本缺失抛 RuntimeError → launch_thread 记 failed（不静默回退 builtin）。"""
    if backend_registry.is_custom_name(getattr(dep, "backend_name", "")):
        return backend_registry.backend_context(dep.backend_name,
                                                getattr(dep, "backend_version", None) or None)
    return {}


def _engine_image(dep: Deployment, bc: dict | None = None) -> str:
    """部署生效引擎镜像：自定义后端→版本镜像；builtin→pin 系统（运行时升级生效）"""
    if bc and bc.get("image"):
        return bc["image"]
    return upgrade_service.current_image("vllm") if dep.engine == "vllm" \
        else upgrade_service.current_image("sglang")


def build_cmd(dep: Deployment, model_path: str, role: str | None = None, idx: int = 0,
              worker_ip: str = "") -> list[str]:
    """拼引擎启动命令。role：PD 分离实例角色（prefill/decode）；None=协同实例。
    worker_ip：本容器 IP（自定义后端 {{worker_ip}} 占位符；容器创建后传入）。

    自定义后端（dep.backend_name 非 builtin）：版本启动命令模板占位符渲染
    （backend_registry.custom_cmd），单机闭环、不支持 PD。

    PD 分离（vLLM ≥0.30 / SGLang ≥0.4.9 参数面，按镜像 v0.33.0 / v0.5.23 核实）：
    - vLLM:  --kv-transfer-config '{"kv_connector": "NixlConnector"|"MooncakeConnector",
                    "kv_role": "kv_producer"|"kv_consumer"}'
             配合 exec 级 env VLLM_NIXL_SIDE_CHANNEL_PORT（每 worker 唯一）+
             VLLM_NIXL_SIDE_CHANNEL_HOST（prefill 侧，decode 据此拉取 KV）
    - SGLang: --disaggregation-mode prefill|decode
                    --disaggregation-transfer-backend nixl|mooncake
                    --disaggregation-bootstrap-port <port>（仅 prefill 侧）
    """
    if backend_registry.is_custom_name(getattr(dep, "backend_name", "")):
        return backend_registry.custom_cmd(dep, model_path, role, idx, worker_ip,
                                           _served_name(dep))
    adv = _adv(dep)
    served = _served_name(dep)
    pd = (dep.pd_mode or "off") == "pd" and role in ("prefill", "decode")
    # PD 模式下每角色实例独立 TP（协同模式沿用 dep.tensor_parallel）
    tp = dep.tensor_parallel
    if pd:
        tp = dep.prefill_tp if role == "prefill" else dep.decode_tp
    if dep.engine == "sglang":
        # 官方 sglang 镜像内只有 python3（无 python 别名）
        cmd = ["python3", "-m", "sglang.launch_server",
               "--model-path", model_path,
               "--host", "0.0.0.0", "--port", "8000",
               "--served-model-name", served,
               "--context-length", str(dep.max_model_len)]
        # 显存静态占比：高级参数优先，否则回退 gpu_mem_util（与旧行为一致）
        mf = adv.get("mem_fraction_static")
        cmd += ["--mem-fraction-static", str(mf if mf not in (None, "") else min(dep.gpu_mem_util, 0.9))]
        if dep.quantization and dep.quantization != "none":
            cmd += ["--quantization", dep.quantization]
        if tp > 1:
            cmd += ["--tp", str(tp)]
        # 部署级扩展 KV 缓存：SGLang HiCache（host 内存二级 KV，GiB）
        hic = _kv_ext(dep).get("hicache_size")
        if hic not in (None, "", 0, "0"):
            cmd += ["--hicache-size", str(hic)]
        if pd:
            cmd += ["--disaggregation-mode", role,
                    "--disaggregation-transfer-backend", _sglang_backend(dep)]
            if role == "prefill":
                cmd += ["--disaggregation-bootstrap-port", str(pd_bootstrap_port(dep, idx))]
        if dep.enable_lora:
            cmd.append("--enable-lora")
        for a in (dep.lora_adapters or []):
            cmd += [f"--lora-paths", f"{a['name']}={a['path']}"]
        if _truthy(adv.get("disable_cuda_graph")):
            cmd.append("--disable-cuda-graph")
        cmd += _extra_argv(adv.get("extra_args"))  # 原始命令行透传（兜底一切）
        return cmd
    # vllm：参数全部拼进 bash -lc 的脚本串（append 成独立 argv 元素不会被 bash -lc 执行）
    parts = [f"vllm serve {model_path}",
             "--host 0.0.0.0 --port 8000",
             f"--served-model-name {served}",
             f"--max-model-len {dep.max_model_len}",
             f"--gpu-memory-utilization {dep.gpu_mem_util}"]
    if dep.quantization and dep.quantization != "none":
        parts.append(f"--quantization {dep.quantization}")
    if tp > 1:
        parts.append(f"--tensor-parallel-size {tp}")
    # 部署级扩展 KV 缓存：vLLM CPU offload（GiB）
    off = _kv_ext(dep).get("cpu_offload_gb")
    if off not in (None, "", 0, "0"):
        parts.append(f"--cpu-offload-gb {off}")
    if pd:
        # JSON 含空格/引号，shlex.quote 防 bash -lc 解析破坏
        kvt = {"kv_connector": "NixlConnector" if _vllm_connector(dep) == "nixl"
               else "MooncakeConnector",
               "kv_role": "kv_producer" if role == "prefill" else "kv_consumer"}
        parts.append(f"--kv-transfer-config {shlex.quote(json.dumps(kvt))}")
    # ---- 高级参数（留空=引擎默认）----
    mns = adv.get("max_num_seqs")
    if mns not in (None, ""):
        parts.append(f"--max-num-seqs {mns}")
    dt = _dtype_cli(adv.get("dtype"))
    if dt:
        parts.append(f"--dtype {dt}")
    if _truthy(adv.get("enforce_eager")):
        parts.append("--enforce-eager")
    if _truthy(adv.get("enable_prefix_caching")):
        parts.append("--enable-prefix-caching")
    if _truthy(adv.get("enable_chunked_prefill")):
        parts.append("--enable-chunked-prefill")
    api_key = str(adv.get("api_key") or "").strip()
    if api_key:
        parts.append(f'--api-key {api_key}')
    if dep.enable_lora:
        parts.append("--enable-lora")
        for a in (dep.lora_adapters or []):
            parts.append(f"--lora-modules {a['name']}={a['path']}")
    extra = str(adv.get("extra_args") or "").strip()
    if extra:
        parts.append(extra)  # 原始命令行透传（bash -lc 内直接生效，兜底一切）
    return ["bash", "-lc", " ".join(parts) + " 2>&1"]


def deployment_endpoint(dep: Deployment) -> str:
    """返回可被 docker 网络内访问的 OpenAI 兼容 base_url（含 /v1）"""
    return _ep_url(dep, "/v1")


def start_replica(dep: Deployment, replica: int, model_path: str) -> str:
    name = deployment_container_name(dep, replica)
    tc.remove_container(name, force=True)
    bc = _backend_context(dep)  # 自定义后端：镜像+env；builtin：空（既有行为）
    # VLLM_ALLOW_RUNTIME_LORA_UPDATING：vLLM>=0.15 的运行期 LoRA 热加载 API
    # （POST /v1/load_lora_adapter、/v1/unload_lora_adapter）默认关闭（官方标注
    # "仅本地开发使用"），平台需要它实现 LoRA 热加载，故显式开启（仅 builtin 注入）。
    base_env = bc.get("env") or {"VLLM_LOGGING_LEVEL": "warning",
                                 "VLLM_ALLOW_RUNTIME_LORA_UPDATING": "1"}
    cid = tc.create_container(
        name=name,
        image=_engine_image(dep, bc),
        gpus=max(dep.tensor_parallel, 1),
        cpus=8, mem_gb=20,
        env={**base_env,
             **(dep.nccl_env or {}),   # NCCL/UCX/NIXL 覆盖（多机 TP / RDMA 调优）
             **cache_service.cache_env_for(dep)},  # 关联缓存服务 → VD_CACHE_ENDPOINT
        labels={"fintune.deployment": str(dep.id), "fintune.type": "inference",
                "fintune.engine": dep.engine,
                **({"fintune.backend": dep.backend_name} if bc else {})},
    )
    cmd = build_cmd(dep, model_path, worker_ip=tc.container_ip(cid))
    tc.start_task(f"infer-{dep.id}-r{replica}", cid, cmd,
                  os.path.join(settings.data_dir, "infer", f"{dep.id}", f"run-r{replica}.log"))
    return cid


def _ep_url(dep, path: str = "") -> str:
    """endpoint 以 IP:port 存储，统一补 http:// 构造 URL（兼容已带 scheme 的值）"""
    ep = dep.endpoint
    if not str(ep).startswith("http"):
        ep = "http://" + ep
    return ep + path


def _probe_engine(base_url: str, engine: str, health_path: str | None = None) -> bool:
    """引擎存活探测（自定义后端→注册表 health_check_path；vLLM /v1/models；SGLang /get_server_info）"""
    try:
        path = health_path or ("/v1/models" if engine == "vllm" else "/get_server_info")
        r = requests.get(base_url + path, timeout=5)
        return r.status_code == 200
    except Exception:
        return False


def _custom_health_path(dep: Deployment) -> str | None:
    """自定义后端的探活路径；builtin/解析失败返回 None（用引擎默认路径）"""
    if backend_registry.is_custom_name(getattr(dep, "backend_name", "")):
        try:
            return backend_registry.backend_context(dep.backend_name,
                                                    getattr(dep, "backend_version", None) or None
                                                    )["health_check_path"]
        except Exception as e:
            log.warning("自定义后端探活路径解析失败，用引擎默认路径: %s", e)
    return None


def wait_ready(dep: Deployment, timeout: int = 600) -> bool:
    hp = _custom_health_path(dep)
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _is_pd(dep):
            if _pd_all_ready(dep):
                return True
        elif dep.endpoint:
            if _probe_engine(_ep_url(dep, ""), dep.engine, hp):
                return True
        time.sleep(5)
    return False


def _pd_all_ready(dep: Deployment) -> bool:
    """PD 全实例就绪：所有 P/D 实例引擎可探测（SGLang 另要求 router 可探测）"""
    p_ips, d_ips = pd_instance_ips(dep)
    for ip in list(p_ips) + list(d_ips):
        if not ip or not _probe_engine(f"http://{ip}:8000", dep.engine):
            return False
    if dep.engine == "sglang" and dep.endpoint:
        if not _probe_engine(_ep_url(dep, ""), "sglang"):
            return False
    return True


def launch(dep: Deployment, model_path: str) -> None:
    from . import prom_service
    ids = []
    if _is_pd(dep):
        # PD 拓扑：P 池 → D 池（container_ids 布局 [p0..pA, d0..dB]，代理寻址依赖此序）
        for i in range(max(1, dep.prefill_replicas or 1)):
            ids.append(start_pd_instance(dep, "prefill", i, model_path))
        for j in range(max(1, dep.decode_replicas or 1)):
            ids.append(start_pd_instance(dep, "decode", j, model_path))
        dep.container_ids = ids  # start_pd_router 按此布局解析 P/D 容器 IP
        # SGLang：router 容器统一入口；vLLM：路由内置 backend 代理，入口=首个 D 实例
        if dep.engine == "sglang":
            ids.append(start_pd_router(dep, model_path))
            rt_ip = tc.container_ip(ids[-1])
            dep.endpoint = f"{rt_ip}:8000" if rt_ip else ""
        else:
            d0 = tc.container_ip(ids[max(1, dep.prefill_replicas or 1)])
            dep.endpoint = f"{d0}:8000" if d0 else ""
    else:
        for i in range(dep.replicas):
            ids.append(start_replica(dep, i, model_path))
        # 等待首副本就绪后登记 endpoint（IP 在创建后即可取）
        for i in range(dep.replicas):
            ip = tc.container_ip(ids[i])
            if ip and not dep.endpoint:
                dep.endpoint = f"{ip}:8000"
    # 登记 Prometheus file_sd target（所有引擎实例 /metrics，与 OpenAI API 同端口 8000）
    targets = [f"{tc.container_ip(c)}:8000" for c in ids if tc.container_ip(c)]
    first = deployment_container_name(dep, 0) if not _is_pd(dep) else pd_instance_name(dep, "prefill", 0)
    prom_service.write_infer_sd(dep.id, dep.engine, first, targets)
    db = SessionLocal()
    try:
        d = db.get(Deployment, dep.id)
        d.container_ids = ids
        d.endpoint = dep.endpoint
        d.status = "creating"  # 就绪由 wait_ready/调度器翻转，避免 running 早于端点可用
        db.commit()
    finally:
        db.close()


def _pd_container_names(dep: Deployment) -> list[str]:
    """PD 部署全部容器名（含 SGLang router；命名兜底，container_ids 缺失也能清干净）"""
    names = [pd_instance_name(dep, "prefill", i) for i in range(max(1, dep.prefill_replicas or 1))]
    names += [pd_instance_name(dep, "decode", j) for j in range(max(1, dep.decode_replicas or 1))]
    if dep.engine == "sglang":
        names.append(pd_router_name(dep))
    return names


def stop(dep: Deployment) -> None:
    from . import prom_service
    if _is_pd(dep):
        targets = set(dep.container_ids or []) | set(_pd_container_names(dep))
    else:
        targets = {deployment_container_name(dep, i) for i in range(max(dep.replicas, 1))}
    for t in targets:
        tc.remove_container(t, force=True)
    prom_service.drop_infer_sd(dep.id)  # 摘除 Prometheus target
    dep.container_ids = []
    dep.endpoint = ""
    db = SessionLocal()
    try:
        d = db.get(Deployment, dep.id)
        d.container_ids = []
        d.endpoint = ""
        d.status = "stopped"
        db.commit()
    finally:
        db.close()


# ---------- 后台启动线程（API start/create 与调度器自动重启共用） ----------

_launch_tokens: dict[int, str] = {}
_launch_lock = threading.Lock()


def issue_launch_token(did: int) -> str:
    """签发新启动令牌（作废旧后台线程，防其超时回写覆盖新状态——竞态防护）"""
    import uuid
    tok = uuid.uuid4().hex
    with _launch_lock:
        _launch_tokens[did] = tok
    return tok


def launch_stale(did: int, tok: str) -> bool:
    with _launch_lock:
        return _launch_tokens.get(did) != tok


def resolve_model_path(asset: ModelAsset, db) -> str:
    """LoRA 资产解析到基座模型路径；其余资产用自身路径。"""
    if asset and asset.type == "lora" and asset.parent_asset_id:
        base = db.get(ModelAsset, asset.parent_asset_id)
        if base:
            return base.path
    return asset.path if asset else ""


def wait_engine_image(dep: Deployment, db2) -> str:
    """引擎镜像按需下载（懒加载）：本地缺失自动后台拉取并等待就绪。

    返回 "ready" 继续建容器；拉取失败/超时回写 failed 并返回 "stop"。
    """
    from . import image_ops
    try:
        img = _engine_image(dep, _backend_context(dep))
    except Exception as e:  # 后端/版本被删等 → 直接 failed（清晰报错）
        dep.status = "failed"
        dep.error = str(e)[:4000]
        db2.commit()
        return "stop"
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


def launch_thread(did: int, note: str = "") -> str:
    """后台线程启动部署：镜像等待 → 建容器 → 就绪等待 → 状态回写。

    启动/停止/重启每次换发新令牌作废旧线程；成功就绪回写 running 并清零
    连续失败计数（自动重启退避重置）。返回本次启动令牌（空=部署不存在）。
    """
    db0 = SessionLocal()
    try:
        dep = db0.get(Deployment, did)
        if not dep:
            return ""
        model_asset_id = dep.model_asset_id
        if note:
            dep.state_message = note
            db0.commit()
    finally:
        db0.close()
    tok = issue_launch_token(did)

    def _run():
        db2 = SessionLocal()
        try:
            dep = db2.get(Deployment, did)
            path = resolve_model_path(db2.get(ModelAsset, model_asset_id), db2)
            if wait_engine_image(dep, db2) != "ready":
                return
            launch(dep, path)
            ready = wait_ready(dep, timeout=1200)
            if launch_stale(did, tok):
                return
            dep = db2.get(Deployment, did)
            if ready:
                dep.status = "running"
                dep.error = ""  # 清除历史错误，避免 DB 显示陈旧 error
                dep.error_count = 0  # 恢复健康：自动重启退避清零
                dep.next_restart_at = None
                dep.state_message = ""
            else:
                dep.status = "failed"
                dep.error = "模型加载超时，请查看日志"
            db2.commit()
        except Exception as e:
            if launch_stale(did, tok):
                return
            dep2 = db2.get(Deployment, did)
            dep2.status = "failed"
            dep2.error = str(e)[:4000]
            db2.commit()
        finally:
            db2.close()

    threading.Thread(target=_run, daemon=True).start()
    return tok


def cleanup_instances(dep: Deployment) -> None:
    """清理部署全部实例容器 + Prometheus target（不写状态——由调用方掌控状态流转）。
    用于自动重启/参数重建：stop() 会强制写 stopped，重建路径不需要。"""
    from . import prom_service
    if _is_pd(dep):
        targets = set(dep.container_ids or []) | set(_pd_container_names(dep))
    else:
        targets = {deployment_container_name(dep, i) for i in range(max(dep.replicas, 1))}
    for t in targets:
        tc.remove_container(t, force=True)
    prom_service.drop_infer_sd(dep.id)
    db = SessionLocal()
    try:
        d = db.get(Deployment, dep.id)
        d.container_ids = []
        d.endpoint = ""
        db.commit()
    finally:
        db.close()


def _lora_targets(dep: Deployment):
    """LoRA 热加载目标 base_url 列表；异常返回 {"error": ...}；未运行返回 None。

    PD 部署（vLLM）：KV 传输要求 P/D 两侧模型一致，热加载必须批量下发到
    全部 P/D 实例；PD（SGLang）引擎无运行期 LoRA API，直接拒绝。
    """
    if not dep.endpoint:
        return None
    if _is_pd(dep):
        if dep.engine == "sglang":
            return {"error": "SGLang PD 部署暂不支持运行期 LoRA 热加载"}
        p_ips, d_ips = pd_instance_ips(dep)
        targets = [f"http://{ip}:8000" for ip in list(p_ips) + list(d_ips) if ip]
        return targets or [_ep_url(dep)]
    return [_ep_url(dep)]


def hot_add_lora(dep: Deployment, name: str, path: str) -> dict:
    """vLLM 运行期热加载 LoRA（无需重启；PD 部署批量下发全部 P/D 实例）

    vLLM >=0.15: POST /v1/load_lora_adapter {lora_name, lora_path}
    中间版本:    PUT /v1/loras/{name} {model_path}
    （0.8.5/0.9.x/0.10-0.14 均无运行期 LoRA API，需 vLLM>=0.15 或 <=0.14 的对应形态）
    """
    targets = _lora_targets(dep)
    if targets is None:
        return {"ok": False, "error": "deployment not running"}
    if isinstance(targets, dict):
        return {"ok": False, "error": targets["error"]}
    fails = []
    for base in targets:
        try:
            r = requests.post(f"{base}/v1/load_lora_adapter",
                              json={"lora_name": name, "lora_path": path}, timeout=180)
            if r.status_code < 400:
                continue
            r2 = requests.put(f"{base}/v1/loras/{name}", json={"model_path": path}, timeout=180)
            if r2.status_code < 400:
                continue
            fails.append(f"{base}: HTTP {r.status_code} {r.text[:200]}")
        except Exception as e:
            fails.append(f"{base}: {e}")
    if fails:
        msg = " | ".join(fails)[:2000]
        # 部署未带 --enable-lora 启动时重试无解，给出明确处置（旧文案"请重试"误导）
        if "LoRA is not enabled" in msg:
            return {"ok": False, "error":
                    "部署未启用 LoRA（enable_lora），无法热加载：请以 enable_lora=true "
                    "重建/更新部署后重试。详情: " + msg}
        return {"ok": False, "error": "LoRA 热加载部分失败（P/D 两侧需一致，请重试）: " + msg}
    return {"ok": True, "targets": len(targets)}


def hot_remove_lora(dep: Deployment, name: str) -> dict:
    targets = _lora_targets(dep)
    if targets is None:
        return {"ok": False, "error": "deployment not running"}
    if isinstance(targets, dict):
        return {"ok": False, "error": targets["error"]}
    fails = []
    for base in targets:
        try:
            r = requests.post(f"{base}/v1/unload_lora_adapter",
                              json={"lora_name": name}, timeout=60)
            if r.status_code < 400:
                continue
            r2 = requests.delete(f"{base}/v1/loras/{name}", timeout=60)
            if r2.status_code < 400:
                continue
            fails.append(f"{base}: HTTP {r.status_code}")
        except Exception as e:
            fails.append(f"{base}: {e}")
    if fails:
        return {"ok": False, "error": "LoRA 卸载部分失败: " + " | ".join(fails)[:1000]}
    return {"ok": True, "status": 200}


def ensure_serving(asset: ModelAsset, db) -> tuple[str, str]:
    """评测 quick-serve：若资产已有运行中部署则复用，否则临时拉起一个"""
    dep = db.query(Deployment).filter(
        Deployment.model_asset_id == asset.id,
        Deployment.status == "running").first()
    if dep:
        return deployment_endpoint(dep), dep.name
    # 临时部署
    import uuid
    name = f"tmp-{asset.name}-{uuid.uuid4().hex[:6]}"
    dep = Deployment(
        project_id=asset.project_id, name=name, model_asset_id=asset.id,
        engine="vllm", quantization="none", tensor_parallel=1, max_model_len=8192,
        gpu_mem_util=0.5, enable_lora=asset.type == "lora",
        lora_adapters=[{"name": "default", "path": asset.path}] if asset.type == "lora" else [],
        replicas=1, status="creating", created_by=asset.created_by,
    )
    db.add(dep)
    db.commit()
    db.refresh(dep)
    model_path = _resolve_model_path(asset, db)
    launch(dep, model_path)
    ready = wait_ready(dep, timeout=900)
    db.refresh(dep)
    if not ready:
        stop(dep)
        raise RuntimeError("quick-serve 启动超时")
    return deployment_endpoint(dep), dep.name


def _resolve_model_path(asset: ModelAsset, db) -> str:
    if asset.type == "lora":
        base = db.get(ModelAsset, asset.parent_asset_id) if asset.parent_asset_id else None
        return base.path if base else asset.path
    return asset.path


# ---------- 引擎指标抓取（vLLM /metrics、SGLang /metrics 或 /get_server_info 兜底） ----------

_last_scrape = 0.0
_infer_fail: dict[int, int] = {}


def _parse_prom(text: str) -> dict:
    """Prometheus 文本 → {metric_name: 数值和}（histogram 桶/和/计数字段除外，另行解析）"""
    out: dict[str, float] = {}
    for line in text.splitlines():
        if not line or line.startswith("#"):
            continue
        name = line.split("{")[0].split(" ")[0].strip()
        if not name or name.endswith(("_bucket", "_sum", "_count", "_created")):
            continue
        try:
            val = float(line.rsplit(" ", 1)[-1])
        except ValueError:
            continue
        out[name] = out.get(name, 0.0) + val
    return out


def _hist_avg(text: str, prefix: str) -> float:
    """histogram 平均（sum/count），秒 → 毫秒。指标名后直接跟 {labels}，中间无空格"""
    p = re.escape(prefix)
    sums = [float(x) for x in re.findall(rf"^{p}_sum\{{[^}}]*\}}\s+([0-9.eE+nan-]+)", text, re.M)]
    counts = [float(x) for x in re.findall(rf"^{p}_count\{{[^}}]*\}}\s+([0-9.eE+nan-]+)", text, re.M)]
    if sums and counts and sum(counts) > 0:
        return sum(sums) / sum(counts) * 1000
    return 0.0


def _find_keys(obj, mapping: dict[str, str]) -> dict:
    """递归遍历 JSON 取指定 key（SGLang /get_server_info 结构随版本变化，按 key 名兜底）"""
    out: dict[str, float] = {}

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in mapping and isinstance(v, (int, float)) and not isinstance(v, bool):
                    out.setdefault(mapping[k], float(v))
                else:
                    walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)

    walk(obj)
    return out


def _scrape_one(dep: Deployment) -> dict | None:
    """抓取单个部署的引擎指标 → {kv, running, waiting, latency_ms}；失败返回 None"""
    base = _ep_url(dep, "")  # http://ip:8000
    try:
        r = requests.get(base + "/metrics", timeout=5)
        if r.status_code == 200 and r.text:
            vals = _parse_prom(r.text)
            if dep.engine == "vllm":
                kv = vals.get("vllm:kv_cache_usage_perc")
                if kv is None:
                    kv = vals.get("vllm_gpu_cache_usage")  # 旧版 vLLM 指标名
                if kv is None:
                    kv = 0.0
                return {
                    "kv": min(100.0, max(0.0, kv) * 100),
                    "running": vals.get("vllm:num_requests_running", 0),
                    "waiting": vals.get("vllm:num_requests_waiting", 0),
                    "latency_ms": _hist_avg(r.text, "vllm:e2e_request_latency_seconds"),
                }
            kv = vals.get("sglang:token_usage")
            if kv is not None:
                return {
                    "kv": min(100.0, max(0.0, kv) * 100),
                    "running": vals.get("sglang:num_running_requests", 0),
                    "waiting": vals.get("sglang:num_queue_requests", 0),
                    "latency_ms": 0,
                }
    except Exception:
        pass
    # SGLang 兜底：/metrics 不可用或指标名变化时查 /get_server_info
    if dep.engine == "sglang":
        try:
            r = requests.get(base + "/get_server_info", timeout=5)
            if r.status_code == 200:
                found = _find_keys(r.json(), {
                    "token_usage": "kv", "num_running_reqs": "running", "num_queue_reqs": "waiting",
                })
                if found.get("kv") is not None:
                    return {
                        "kv": min(100.0, max(0.0, found["kv"]) * 100),
                        "running": found.get("running", 0),
                        "waiting": found.get("waiting", 0),
                        "latency_ms": 0,
                    }
        except Exception:
            pass
    return None


def scrape_metrics(db, force: bool = False):
    """调度器每 tick 调用：约 30s 探活一次运行中部署的引擎指标端点；
    连续 3 次抓取失败触发 infer_down 事件（引擎挂死但端口仍监听时，
    Prometheus 的 up{job="inference"} 发现不了，由本探活兜底）。
    指标本体由 Prometheus docker_sd 直接抓取 /metrics 入库，不再写 DB。"""
    global _last_scrape
    now = time.time()
    if not force and now - _last_scrape < 30:
        return
    _last_scrape = now
    from . import alert_service
    deps = db.query(Deployment).filter(Deployment.status == "running").all()
    for dep in deps:
        if not dep.endpoint:
            continue
        # 自定义后端：无 vLLM/SGLang 指标口径，探活走注册表 health_check_path
        # （缺指标不再误判 infer_down；挂死时经健康路径 3 连败兜底）
        if backend_registry.is_custom_name(getattr(dep, "backend_name", "")):
            hp = _custom_health_path(dep)
            if _probe_engine(_ep_url(dep, ""), dep.engine, hp):
                _infer_fail[dep.id] = 0
                continue
            n = _infer_fail.get(dep.id, 0) + 1
            _infer_fail[dep.id] = n
            if n == 3:
                alert_service.trigger_event(db, "infer_down", f"deploy:{dep.id}",
                                            f"推理服务不可用：{dep.name}",
                                            f"健康检查 {hp or '/v1/models'} @ {dep.endpoint} "
                                            f"连续 3 次不可达（服务可能挂死），请查看部署日志")
            continue
        m = _scrape_one(dep)
        if m is None:
            n = _infer_fail.get(dep.id, 0) + 1
            _infer_fail[dep.id] = n
            if n == 3:
                alert_service.trigger_event(db, "infer_down", f"deploy:{dep.id}",
                                            f"推理服务不可用：{dep.name}",
                                            f"引擎指标端点 {dep.endpoint} 连续 3 次不可达（引擎可能挂死），请查看部署日志")
            continue
        _infer_fail[dep.id] = 0

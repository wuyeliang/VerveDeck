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

log = logging.getLogger("inference")

_counter = 0
_counter_lock = threading.Lock()


def _next_rr(deployment_id: int, replicas: int) -> int:
    global _counter
    with _counter_lock:
        _counter += 1
        return _counter % replicas


def deployment_container_name(dep: Deployment, replica: int = 0) -> str:
    return f"ft-infer-{dep.id}" if dep.replicas <= 1 else f"ft-infer-{dep.id}-r{replica}"


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


def build_cmd(dep: Deployment, model_path: str) -> list[str]:
    adv = _adv(dep)
    served = _served_name(dep)
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
        if dep.tensor_parallel > 1:
            cmd += ["--tp", str(dep.tensor_parallel)]
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
    if dep.tensor_parallel > 1:
        parts.append(f"--tensor-parallel-size {dep.tensor_parallel}")
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
    cid = tc.create_container(
        name=name,
        image=upgrade_service.current_image("vllm") if dep.engine == "vllm"
        else upgrade_service.current_image("sglang"),
        gpus=max(dep.tensor_parallel, 1),
        cpus=8, mem_gb=20,
        # VLLM_ALLOW_RUNTIME_LORA_UPDATING：vLLM>=0.15 的运行期 LoRA 热加载 API
        # （POST /v1/load_lora_adapter、/v1/unload_lora_adapter）默认关闭（官方标注
        # "仅本地开发使用"），平台需要它实现 LoRA 热加载，故显式开启。
        env={"VLLM_LOGGING_LEVEL": "warning",
             "VLLM_ALLOW_RUNTIME_LORA_UPDATING": "1"},
        labels={"fintune.deployment": str(dep.id), "fintune.type": "inference",
                "fintune.engine": dep.engine},
    )
    cmd = build_cmd(dep, model_path)
    tc.start_task(f"infer-{dep.id}-r{replica}", cid, cmd,
                  os.path.join(settings.data_dir, "infer", f"{dep.id}", f"run-r{replica}.log"))
    return cid


def _ep_url(dep, path: str = "") -> str:
    """endpoint 以 IP:port 存储，统一补 http:// 构造 URL（兼容已带 scheme 的值）"""
    ep = dep.endpoint
    if not str(ep).startswith("http"):
        ep = "http://" + ep
    return ep + path


def wait_ready(dep: Deployment, timeout: int = 600) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        if dep.endpoint:
            try:
                r = requests.get(_ep_url(dep, "/v1/models"), timeout=5)
                if r.status_code == 200:
                    return True
            except Exception:
                pass
        time.sleep(5)
    return False


def launch(dep: Deployment, model_path: str) -> None:
    from . import prom_service
    ids = []
    for i in range(dep.replicas):
        ids.append(start_replica(dep, i, model_path))
    # 等待首副本就绪后登记 endpoint（IP 在创建后即可取）
    targets = []
    for i in range(dep.replicas):
        ip = tc.container_ip(ids[i])
        if ip:
            targets.append(f"{ip}:8000")
            if i == 0:
                dep.endpoint = targets[0]
    dep.container_ids = ids
    # 登记 Prometheus file_sd target（/metrics 与 OpenAI API 同端口 8000）
    prom_service.write_infer_sd(dep.id, dep.engine, deployment_container_name(dep, 0), targets)
    db = SessionLocal()
    try:
        d = db.get(Deployment, dep.id)
        d.container_ids = ids
        d.endpoint = dep.endpoint
        d.status = "creating"  # 就绪由 wait_ready/调度器翻转，避免 running 早于端点可用
        db.commit()
    finally:
        db.close()


def stop(dep: Deployment) -> None:
    from . import prom_service
    for i in range(max(dep.replicas, 1)):
        tc.remove_container(deployment_container_name(dep, i), force=True)
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


def hot_add_lora(dep: Deployment, name: str, path: str) -> dict:
    """vLLM 运行期热加载 LoRA（无需重启）

    vLLM >=0.15: POST /v1/load_lora_adapter {lora_name, lora_path}
    中间版本:    PUT /v1/loras/{name} {model_path}
    （0.8.5/0.9.x/0.10-0.14 均无运行期 LoRA API，需 vLLM>=0.15 或 <=0.14 的对应形态）
    """
    if not dep.endpoint:
        return {"ok": False, "error": "deployment not running"}
    base = _ep_url(dep)
    try:
        r = requests.post(f"{base}/v1/load_lora_adapter",
                          json={"lora_name": name, "lora_path": path}, timeout=180)
        if r.status_code < 400:
            return {"ok": True, "status": r.status_code, "body": r.text[:500]}
        r2 = requests.put(f"{base}/v1/loras/{name}", json={"model_path": path}, timeout=180)
        if r2.status_code < 400:
            return {"ok": True, "status": r2.status_code, "body": r2.text[:500]}
        return {"ok": False, "status": r.status_code, "body": r.text[:500]}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def hot_remove_lora(dep: Deployment, name: str) -> dict:
    if not dep.endpoint:
        return {"ok": False, "error": "deployment not running"}
    base = _ep_url(dep)
    try:
        r = requests.post(f"{base}/v1/unload_lora_adapter",
                          json={"lora_name": name}, timeout=60)
        if r.status_code < 400:
            return {"ok": True, "status": r.status_code}
        r2 = requests.delete(f"{base}/v1/loras/{name}", timeout=60)
        return {"ok": r2.status_code < 400, "status": r2.status_code}
    except Exception as e:
        return {"ok": False, "error": str(e)}


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

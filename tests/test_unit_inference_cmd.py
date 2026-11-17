# -*- coding: utf-8 -*-
"""推理高级参数（#96）单测：build_cmd 注入 vLLM/SGLang 高级参数 + 宿主/功率快照

纯逻辑 + 内存替身，不触真实 Prometheus / Docker / 网络。
留空=引擎默认：存量部署（advanced 为空）的启动命令与旧行为逐字节一致。
"""
from app import models as M
from app.services import inference_service as inf
from app.services import prom_service as ps
from app.services import gpu_service as gs
from app.api.inference import _served_name as api_served


def _dep(engine, **kw):
    base = dict(name="m1", max_model_len=8192, gpu_mem_util=0.85,
                quantization="none", tensor_parallel=1,
                enable_lora=False, lora_adapters=[], advanced={})
    base.update(engine=engine, **kw)
    return M.Deployment(**base)


# ---------------- vLLM ----------------

def test_build_cmd_vllm_legacy_unchanged():
    """advanced 留空 → 命令与旧行为一致（无高级参数、served=部署名）"""
    dep = _dep("vllm", name="svc-1", max_model_len=4096, gpu_mem_util=0.8)
    cmd = inf.build_cmd(dep, "/models/svc-1")
    assert cmd[:2] == ["bash", "-lc"]
    s = cmd[2]
    assert "vllm serve /models/svc-1" in s
    assert "--served-model-name svc-1" in s
    assert "--max-model-len 4096" in s
    assert "--gpu-memory-utilization 0.8" in s
    for flag in ("--max-num-seqs", "--dtype", "--enforce-eager",
                 "--enable-prefix-caching", "--enable-chunked-prefill",
                 "--api-key"):
        assert flag not in s, flag


def test_build_cmd_vllm_advanced_all():
    dep = _dep("vllm", quantization="awq", tensor_parallel=2, advanced={
        "max_num_seqs": 256, "dtype": "bf16", "enforce_eager": True,
        "enable_prefix_caching": True, "enable_chunked_prefill": True,
        "served_model_name": "served-x", "api_key": "sk-abc",
        "extra_args": "--max-num-batched-tokens 8192",
    })
    s = inf.build_cmd(dep, "/m1")[2]
    assert "--max-num-seqs 256" in s
    assert "--dtype bfloat16" in s
    assert "--enforce-eager" in s
    assert "--enable-prefix-caching" in s
    assert "--enable-chunked-prefill" in s
    assert "--served-model-name served-x" in s
    assert "--api-key sk-abc" in s
    assert "--max-num-batched-tokens 8192" in s
    assert "--quantization awq" in s
    assert "--tensor-parallel-size 2" in s


def test_build_cmd_vllm_dtype_mapping_and_off_switches():
    dep = _dep("vllm", advanced={"dtype": "fp16", "enforce_eager": "false",
                                 "enable_prefix_caching": False, "max_num_seqs": 64})
    s = inf.build_cmd(dep, "/m1")[2]
    assert "--dtype float16" in s
    assert "--max-num-seqs 64" in s
    assert "--enforce-eager" not in s          # "false" 视为关
    assert "--enable-prefix-caching" not in s
    assert "--api-key" not in s


# ---------------- SGLang ----------------

def test_build_cmd_sglang_legacy_unchanged():
    dep = _dep("sglang", name="svc-2", max_model_len=4096, gpu_mem_util=0.85)
    cmd = inf.build_cmd(dep, "/models/svc-2")
    assert cmd[0] == "python3" and cmd[1] == "-m" and cmd[2] == "sglang.launch_server"
    joined = " ".join(cmd)
    assert "--served-model-name svc-2" in joined
    assert "--mem-fraction-static 0.85" in joined     # min(0.85, 0.9)
    assert "--disable-cuda-graph" not in cmd


def test_build_cmd_sglang_advanced_all():
    dep = _dep("sglang", tensor_parallel=4, gpu_mem_util=0.9, enable_lora=True,
               lora_adapters=[{"name": "a", "path": "/a"}], advanced={
                   "mem_fraction_static": 0.7, "disable_cuda_graph": True,
                   "extra_args": "--schedule-policy lpm",
               })
    cmd = inf.build_cmd(dep, "/m1")
    joined = " ".join(cmd)
    assert "--mem-fraction-static 0.7" in joined          # 覆盖 gpu_mem_util
    assert "--disable-cuda-graph" in cmd
    assert "--tp 4" in joined
    assert "--lora-paths" in joined
    assert "--schedule-policy" in cmd and "lpm" in cmd


def test_build_cmd_extra_argv_quoted():
    """extra_args 经 shlex 切分（引号/空格正确处理）"""
    dep = _dep("sglang", advanced={"extra_args": '--reasoning-parser "deepseek-v3" -v'})
    cmd = inf.build_cmd(dep, "/m1")
    assert "--reasoning-parser" in cmd and "deepseek-v3" in cmd and "-v" in cmd


# ---------------- served_model_name 联动 ----------------

def test_served_name_override_and_fallback():
    assert api_served(_dep("vllm", name="a", advanced={})) == "a"
    assert api_served(_dep("vllm", name="a", advanced={"served_model_name": "  "})) == "a"
    assert api_served(_dep("vllm", name="a",
                           advanced={"served_model_name": "ext-model"})) == "ext-model"


def test_infer_service_served_name_matches_api():
    dep = _dep("vllm", name="a", advanced={"served_model_name": "ext"})
    assert inf._served_name(dep) == api_served(dep) == "ext"


# ---------------- 宿主快照（node-exporter） ----------------

def _fake_instant_by_query(values: dict):
    def fake(promql):
        for key, v in values.items():
            if key in promql:
                return [{"metric": {"instance": "node-1:9100"}, "value": [0, str(v)]}]
        return []
    return fake


def test_host_snapshot_values(monkeypatch):
    monkeypatch.setattr(ps, "prom_instant", _fake_instant_by_query({
        "node_cpu_seconds_total": 12.5,
        "node_memory_MemAvailable": 33.3,
        "node_filesystem_avail": 77.7,
        "node_filesystem_size": 500,
        "node_network_receive": 1024,
        "node_network_transmit": 2048,
    }))
    out = ps.host_snapshot("node-1")
    assert out["cpu_pct"] == 12.5
    assert out["mem_pct"] == 33.3
    assert out["disk_pct"] == 77.7
    assert out["net_rx_bps"] == 1024.0
    assert out["net_tx_bps"] == 2048.0
    assert out["disk_total_gb"] == 500.0 / (1024 ** 3)


def test_host_snapshot_instance_filter(monkeypatch):
    def fake(promql):
        return [{"metric": {"instance": "node-1:9100"}, "value": [0, "1"]},
                {"metric": {"instance": "node-2:9100"}, "value": [0, "9"]}]
    monkeypatch.setattr(ps, "prom_instant", fake)
    assert ps.host_snapshot("node-2")["cpu_pct"] == 9.0
    assert ps.host_snapshot("node-1")["cpu_pct"] == 1.0
    assert ps.host_snapshot(None)["cpu_pct"] == 1.0   # 未指定回退首条


def test_host_snapshot_unreachable(monkeypatch):
    def boom(promql):
        raise RuntimeError("prometheus down")
    monkeypatch.setattr(ps, "prom_instant", boom)
    out = ps.host_snapshot("node-1")
    assert out == {"cpu_pct": None, "mem_pct": None, "disk_pct": None,
                   "net_rx_bps": None, "net_tx_bps": None, "disk_total_gb": None}


# ---------------- GPU 快照：功耗限额 / remap ----------------

def test_gpu_snapshot_power_limit_and_remap(monkeypatch):
    def one(idx, val):
        return [{"metric": {"gpu": str(idx)}, "value": [0, str(val)]}]

    def fake(promql):
        if promql == "DCGM_FI_DEV_GPU_UTIL":
            return one(0, 55)
        if promql == "DCGM_FI_DEV_FB_USED":
            return one(0, 1024)
        if promql == "DCGM_FI_DEV_FB_FREE":
            return one(0, 2048)
        if promql == "DCGM_FI_DEV_POWER_USAGE":
            return one(0, 120)
        if promql == "DCGM_FI_DEV_POWER_MGMT_LIMIT":
            return one(0, 250)
        if promql == "DCGM_FI_DEV_ROW_REMAP_PENDING":
            return one(0, 1)
        return []
    monkeypatch.setattr(ps, "prom_instant", fake)
    out = ps.gpu_snapshot(node=None)
    assert len(out) == 1
    assert out[0]["util"] == 55
    assert out[0]["mem_total"] == 3072
    assert out[0]["power"] == 120
    assert out[0]["power_limit"] == 250
    assert out[0]["remap"] == 1


# ---------------- gpu-agent 环境采集（agent.meta） ----------------

def test_gpu_read_meta(monkeypatch, tmp_path):
    # #103 起 meta 含 ib_devices 键（RDMA 设备名，宿主无 RDMA 时为空）
    f = tmp_path / "agent.meta"
    f.write_text("driver=550.54.15\ncuda=12.4\nib_devices=uverbs0,uverbs1,rdma_cm\n")
    monkeypatch.setattr(gs, "META_FILE", str(f))
    assert gs.read_meta() == {"driver": "550.54.15", "cuda": "12.4",
                              "ib_devices": "uverbs0,uverbs1,rdma_cm"}


def test_gpu_read_meta_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(gs, "META_FILE", str(tmp_path / "nope"))
    assert gs.read_meta() == {"driver": "", "cuda": "", "ib_devices": ""}

# -*- coding: utf-8 -*-
"""LMCache 编排单测（#45）：render_spec 命令/镜像/env 渲染、L2 凭据走 env 不进命令行、
spec_digest 漂移摘要、旧路径/逻辑型兼容。

纯函数级，不起容器不触网。
本地可跑：PYTHONPATH=platform/backend python3 -m pytest tests/test_unit_cache_orchestrated.py --noconftest
"""
import json
import re

from app import models as M
from app.services import cache_service as cs


def _cs(**kw):
    base = dict(name="c", description="", image="", command="", env={}, port=0,
                cache_type="memory", capacity_gb=0, status="stopped",
                state_message="", container_id="", endpoint="", created_by=1,
                provider="lmcache", provider_version="v0.5.4", mode="managed",
                config={}, metrics_port=0, restart_on_error=True, healthy=False,
                spec_digest="")
    base.update(kw)
    return M.CacheService(**base)


def _l2_jsons(cmd: str) -> list[dict]:
    """从渲染命令中提取各 --l2-adapter 的 JSON 参数。"""
    return [json.loads(m) for m in re.findall(r"--l2-adapter (\{.*?\})", cmd)]


# ---------- render_spec：LMCache 编排型 ----------

def test_render_orchestrated_defaults():
    spec = cs.render_spec(_cs(config={"ram_size": 32}))
    assert spec["image"] == "lmcache/vllm-openai:v0.5.4"
    cmd = spec["command"]
    for frag in ("lmcache server --host 0.0.0.0 --port 5555",
                 "--l1-size-gb 32", "--chunk-size 256",
                 "--http-host 0.0.0.0 --http-port 8080",
                 "--supported-transfer-mode auto", "--worker-reap-timeout-seconds 60",
                 "--eviction-policy LRU", "--eviction-trigger-watermark 0.85",
                 "--eviction-ratio 0.1"):
        assert frag in cmd, f"缺少 {frag}"
    assert spec["env"]["PYTHONHASHSEED"] == "0"
    assert spec["port"] == 5555 and spec["metrics_port"] == 8080
    assert spec["mem_gb"] == 36  # 32 + 4 开销


def test_render_orchestrated_custom_version_image_ports():
    c = _cs(port=9000, metrics_port=9001, provider_version="v0.5.2",
            config={"ram_size": 8, "image": "reg.example.com/lmcache:custom"})
    spec = cs.render_spec(c)
    assert spec["image"] == "reg.example.com/lmcache:custom"
    assert "--port 9000" in spec["command"] and "--http-port 9001" in spec["command"]
    assert "--l1-size-gb 8" in spec["command"]
    assert spec["mem_gb"] == 12  # 8 + 4


def test_render_orchestrated_l2_fs_native():
    c = _cs(config={"l2_storages": [
        {"backend": "fs_native",
         "params": {"base_path": "/data/kv", "max_capacity_gb": 500,
                    "num_workers": 4, "use_odirect": True}}]})
    spec = cs.render_spec(c)
    l2s = _l2_jsons(spec["command"])
    assert len(l2s) == 1 and l2s[0]["type"] == "fs_native"
    assert l2s[0]["base_path"] == "/data/kv" and l2s[0]["max_capacity_gb"] == 500
    # fs_native 无凭据，env 只有 PYTHONHASHSEED
    assert spec["env"] == {"PYTHONHASHSEED": "0"}


def test_render_orchestrated_l2_resp_credentials_via_env():
    c = _cs(config={"l2_storages": [
        {"backend": "resp",
         "params": {"host": "redis.local", "port": 6379,
                    "username": "u1", "password": "s3cret"}}]})
    spec = cs.render_spec(c)
    l2s = _l2_jsons(spec["command"])
    assert l2s[0]["type"] == "resp"
    assert l2s[0]["host"] == "redis.local" and l2s[0]["port"] == 6379
    # 凭据不进命令行 JSON，只进 env
    assert "password" not in l2s[0] and "username" not in l2s[0]
    assert "s3cret" not in spec["command"]
    assert spec["env"]["LMCACHE_RESP_USERNAME"] == "u1"
    assert spec["env"]["LMCACHE_RESP_PASSWORD"] == "s3cret"


def test_render_orchestrated_l2_s3_credentials_via_env():
    c = _cs(config={"l2_storages": [
        {"backend": "s3",
         "params": {"s3_endpoint": "https://oss.example.com", "s3_region": "cn-x",
                    "aws_access_key_id": "AKID", "aws_secret_access_key": "SKRET",
                    "disable_tls": False}}]})
    spec = cs.render_spec(c)
    l2s = _l2_jsons(spec["command"])
    assert l2s[0]["s3_endpoint"].startswith("https://")
    assert "SKRET" not in spec["command"] and "AKID" not in spec["command"]
    assert spec["env"]["AWS_ACCESS_KEY_ID"] == "AKID"
    assert spec["env"]["AWS_SECRET_ACCESS_KEY"] == "SKRET"


def test_render_orchestrated_parameters_and_env_override():
    c = _cs(config={"ram_size": 16, "parameters": ["--ttl 3600", "  --flag2 v  "],
                    "env": {"LMCACHE_X": "1"}})
    spec = cs.render_spec(c)
    assert spec["command"].endswith("--ttl 3600 --flag2 v")
    assert spec["env"]["LMCACHE_X"] == "1"
    assert spec["env"]["PYTHONHASHSEED"] == "0"


def test_render_orchestrated_multi_l2_order():
    c = _cs(config={"l2_storages": [
        {"backend": "fs_native", "params": {"base_path": "/a"}},
        {"backend": "resp", "params": {"host": "h"}}]})
    spec = cs.render_spec(c)
    l2s = _l2_jsons(spec["command"])
    assert [x["type"] for x in l2s] == ["fs_native", "resp"]


# ---------- render_spec：旧路径 / 逻辑型（向后兼容） ----------

def test_render_legacy_top_level():
    c = _cs(image="example/cache:1", command="serve --x", env={"A": "B"}, port=6000)
    spec = cs.render_spec(c)
    assert spec["image"] == "example/cache:1"
    assert spec["command"] == "serve --x"
    assert spec["env"] == {"A": "B"}
    assert spec["port"] == 6000 and spec["metrics_port"] == 0 and spec["mem_gb"] == 8


def test_render_logical_empty():
    spec = cs.render_spec(_cs())  # config 空 + image 空 = 逻辑型
    assert spec["image"] == "" and spec["command"] == "" and spec["env"] == {}


def test_is_orchestrated():
    assert cs.is_orchestrated(_cs(config={"ram_size": 16}))
    assert not cs.is_orchestrated(_cs())
    assert not cs.is_orchestrated(_cs(config={}))


# ---------- spec_digest：漂移摘要 ----------

def test_spec_digest_stable_and_sensitive():
    c = _cs(provider_version="v0.5.4", config={"ram_size": 16})
    d1, d2 = cs.spec_digest_of(c), cs.spec_digest_of(c)
    assert d1 == d2 and len(d1) == 16
    c.config = {"ram_size": 32}
    assert cs.spec_digest_of(c) != d1
    c2 = _cs(provider_version="v0.5.2", config={"ram_size": 16})
    assert cs.spec_digest_of(c2) != d1  # 版本变化同样漂移


def test_spec_digest_legacy_stable():
    c = _cs(image="example/cache:1")  # config 空
    assert len(cs.spec_digest_of(c)) == 16
    c.command = "serve --y"  # 旧路径字段不进摘要（旧路径靠自动重启，不靠漂移）
    assert cs.spec_digest_of(c)

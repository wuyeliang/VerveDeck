# -*- coding: utf-8 -*-
"""推理部署 API：部署管理 / OpenAI 兼容代理（鉴权×护栏×流式）/ 多 LoRA 热加载 / 扩缩容

依赖常驻部署（qwen25-05b-base, running）。本文件真实创建 2 个部署：
- test_deploy_advanced_params_real：验证 advanced 参数被真实 vLLM 容器接受（等终态+chat）
- test_update_placement_fields_real：放置/运行环境新字段 update 200 回显（不等终态）
均与常驻部署并行（占用空闲卡），finally 内 stop+delete 清理，不污染常驻部署。
"""
import time

import pytest
import requests

from conftest import (BASE, H, SUF, DEFAULT_PROJECT, BASE_MODEL_ASSET_ID,
                      DEPLOY_ID, DEPLOY_NAME)


def _lora_asset(admin_tok):
    """默认项目中带 parent 的 LoRA 资产（E2E 训练产物优先）"""
    import os
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT, "mtype": "lora"},
                        headers=H(admin_tok), timeout=15).json()
    cands = [a for a in rows if a.get("parent_name") and os.path.isdir(a["path"])]
    with_job = [a for a in cands if a.get("job_id")]
    return (with_job or cands or [None])[0]


@pytest.fixture(scope="module")
def itok(admin_tok, api_key, tenant_a):
    return {"jwt": admin_tok, "key": api_key,
            "other_key_proj": tenant_a["proj_id"],
            "dev_tok": tenant_a["dev"][2]}


def _mk_key(admin_tok, proj_id, name):
    r = requests.post(f"{BASE}/api/admin/projects/{proj_id}/api-keys",
                      headers=H(admin_tok), timeout=15, json={"name": name})
    r.raise_for_status()
    return r.json()["key"]


def _guard_rule(admin_tok, pattern, scope="input", project_id=None, enabled=True):
    body = {"pattern": pattern, "scope": scope, "enabled": enabled}
    if project_id is not None:
        body["project_id"] = project_id
    r = requests.post(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok),
                      timeout=15, json=body)
    r.raise_for_status()
    return r.json()["id"]


def _drop_rule(admin_tok, rid):
    requests.delete(f"{BASE}/api/admin/guard-rules/{rid}", headers=H(admin_tok), timeout=15)


def _chat(tok=None, api_key=None, name=DEPLOY_NAME, content="只回复两个字: 好的",
          stream=False, extra=None):
    body = {"model": name, "messages": [{"role": "user", "content": content}],
            "max_tokens": 24, "temperature": 0}
    if stream:
        body["stream"] = True
    if extra:
        body.update(extra)
    headers = {}
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    if api_key:
        headers["x-api-key"] = api_key
    return requests.post(f"{BASE}/v1/{name}/chat/completions",
                         headers=headers, json=body, timeout=180)


# ---------- 代理鉴权 ----------

def test_proxy_no_auth_401():
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models", timeout=15)
    assert r.status_code == 401


def test_proxy_bad_apikey_401(itok):
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models",
                     headers={"x-api-key": "ofk_000invalid"}, timeout=15)
    assert r.status_code == 401


def test_proxy_wrong_project_key_401(admin_tok, itok):
    """其他项目的 API Key 不能访问本项目部署"""
    key = _mk_key(admin_tok, itok["other_key_proj"], f"other-{SUF}")
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models",
                     headers={"x-api-key": key}, timeout=15)
    assert r.status_code == 401


def test_proxy_apikey_models_200(itok):
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models",
                     headers={"x-api-key": itok["key"]}, timeout=15)
    assert r.status_code == 200


def test_proxy_jwt_models_200(itok):
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models",
                     headers=H(itok["jwt"]), timeout=15)
    assert r.status_code == 200


def test_proxy_jwt_no_project_access_401(itok):
    """无该项目访问权的 JWT 不能代理"""
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models",
                     headers=H(itok["dev_tok"]), timeout=15)
    assert r.status_code == 401


def test_proxy_unknown_deploy_404(itok):
    r = requests.get(f"{BASE}/v1/no-such-deploy-{SUF}/models",
                     headers=H(itok["jwt"]), timeout=15)
    assert r.status_code == 404


def test_proxy_garbage_jwt_401():
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models",
                     headers={"Authorization": "Bearer not.a.jwt"}, timeout=15)
    assert r.status_code == 401


# ---------- 代理功能 ----------

def test_proxy_chat_nonstream(itok):
    r = _chat(tok=itok["jwt"])
    assert r.status_code == 200, r.text[:300]
    content = r.json()["choices"][0]["message"]["content"]
    assert content.strip()


def test_proxy_chat_apikey(itok):
    r = _chat(api_key=itok["key"])
    assert r.status_code == 200, r.text[:300]
    assert r.json()["choices"][0]["message"]["content"]


def test_proxy_chat_stream(itok):
    r = _chat(tok=itok["jwt"], stream=True)
    assert r.status_code == 200
    text = r.text
    assert "data: " in text
    assert "choices" in text


def test_proxy_stream_done_marker(itok):
    r = _chat(tok=itok["jwt"], stream=True)
    assert "[DONE]" in r.text


def test_proxy_models_contains_name(itok):
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models", headers=H(itok["jwt"]),
                     timeout=15)
    names = [m["id"] for m in r.json().get("data", [])]
    assert DEPLOY_NAME in names


def test_proxy_usage_fields(itok):
    r = _chat(tok=itok["jwt"])
    body = r.json()
    assert "usage" in body
    assert body["usage"].get("total_tokens", 0) > 0


def test_proxy_unknown_model_normalized_to_served(itok):
    # #96 行为变更（ac7907c）：代理转发前把请求体 model 归一为部署的
    # served_model_name（存量部署 served==部署名），客户端任意 model 名均可用
    # /v1/<部署名> 路径访问——不再 404 透传，而是 200 且响应 model 为归一名。
    r = _chat(tok=itok["jwt"], extra={"model": "no-such-model-x"})
    assert r.status_code == 200, f"未知 model 应被归一为 served 名后正常完成: {r.status_code} {r.text[:200]}"
    assert r.json()["model"] == DEPLOY_NAME, f"响应 model 应为归一名: {r.json().get('model')}"


def test_proxy_bad_json_400(itok):
    r = requests.post(f"{BASE}/v1/{DEPLOY_NAME}/chat/completions",
                      headers=H(itok["jwt"]),
                      data=b"{not json", timeout=30)
    assert r.status_code in (400, 422)


def test_proxy_root_path(itok):
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/", headers=H(itok["jwt"]), timeout=15)
    assert r.status_code in (200, 404, 405)


# ---------- 内容护栏 ----------

def test_guard_input_blocks_451(admin_tok, itok):
    secret = f"500gs{SUF}"
    rid = _guard_rule(admin_tok, secret, scope="input")
    try:
        r = _chat(tok=itok["jwt"], content=f"请原样重复: {secret}")
        assert r.status_code == 451, f"输入护栏应 451: {r.status_code} {r.text[:200]}"
    finally:
        _drop_rule(admin_tok, rid)


def test_guard_removed_passes(admin_tok, itok):
    secret = f"500gs2{SUF}"
    rid = _guard_rule(admin_tok, secret, scope="input")
    _drop_rule(admin_tok, rid)
    r = _chat(tok=itok["jwt"], content=f"请重复 {secret} 这个词")
    assert r.status_code == 200


def test_guard_output_scope_not_blocked(admin_tok, itok):
    """output 规则只记录日志不拦截"""
    secret = f"500gs3{SUF}"
    rid = _guard_rule(admin_tok, secret, scope="output")
    try:
        r = _chat(tok=itok["jwt"], content="请说你好")
        assert r.status_code == 200
    finally:
        _drop_rule(admin_tok, rid)


def test_guard_disabled_not_blocked(admin_tok, itok):
    secret = f"500gs4{SUF}"
    rid = _guard_rule(admin_tok, secret, scope="input", enabled=False)
    try:
        r = _chat(tok=itok["jwt"], content=f"重复 {secret}")
        assert r.status_code == 200
    finally:
        _drop_rule(admin_tok, rid)


def test_guard_other_project_scoped(admin_tok, itok):
    """项目级规则只对该项目部署生效"""
    proj = _mk_proj(admin_tok)
    secret = f"500gs5{SUF}"
    rid = _guard_rule(admin_tok, secret, scope="input", project_id=proj)
    try:
        r = _chat(tok=itok["jwt"], content=f"重复 {secret}")
        assert r.status_code == 200, "其他项目规则不应拦截本项目部署"
    finally:
        _drop_rule(admin_tok, rid)


def test_guard_case_insensitive(admin_tok, itok):
    secret = f"500GSUPPER{SUF}"
    rid = _guard_rule(admin_tok, secret, scope="input")
    try:
        r = _chat(tok=itok["jwt"], content=f"重复 {secret.lower()}")
        assert r.status_code == 451, "护栏正则应大小写不敏感"
    finally:
        _drop_rule(admin_tok, rid)


def test_guard_regex_pattern(admin_tok, itok):
    rid = _guard_rule(admin_tok, r"1[3-9]\d{9}", scope="input")
    try:
        r = _chat(tok=itok["jwt"], content="我的号码是 13912345678 谢谢")
        assert r.status_code == 451, "正则护栏应拦截手机号"
    finally:
        _drop_rule(admin_tok, rid)


def test_guard_empty_content_ok(admin_tok, itok):
    r = requests.post(f"{BASE}/v1/{DEPLOY_NAME}/chat/completions",
                      headers=H(admin_tok),
                      json={"model": DEPLOY_NAME,
                            "messages": [{"role": "user", "content": ""}],
                            "max_tokens": 8}, timeout=60)
    assert r.status_code in (200, 400)  # 空输入不触发护栏异常


def _mk_proj(admin_tok):
    r = requests.post(f"{BASE}/api/admin/tenants", headers=H(admin_tok), timeout=15,
                      json={"name": f"500gten-{SUF}"})
    r.raise_for_status()
    r = requests.post(f"{BASE}/api/admin/projects", headers=H(admin_tok), timeout=15,
                      json={"tenant_id": r.json()["id"], "name": f"500gproj-{SUF}"})
    r.raise_for_status()
    return r.json()["id"]


# ---------- 部署管理 ----------

def test_deploys_list(admin_tok):
    rows = requests.get(f"{BASE}/api/inference/deployments",
                        headers=H(admin_tok), timeout=15).json()
    assert any(d["id"] == DEPLOY_ID for d in rows)


def test_deploys_list_filter(admin_tok):
    rows = requests.get(f"{BASE}/api/inference/deployments",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    assert all(d["project_id"] == DEFAULT_PROJECT for d in rows)


def test_deploy_get(admin_tok):
    r = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert r.json()["name"] == DEPLOY_NAME


def test_deploy_get_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/inference/deployments/999999",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_deploy_brief_fields(admin_tok):
    d = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                     headers=H(admin_tok), timeout=15).json()
    for k in ("id", "name", "model", "model_asset_id", "engine", "quantization",
              "tensor_parallel", "max_model_len", "gpu_mem_util", "enable_lora",
              "lora_adapters", "replicas", "endpoint", "status", "error",
              "openai_base_url"):
        assert k in d, f"缺字段 {k}"


def test_deploy_openai_base_url(admin_tok):
    d = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                     headers=H(admin_tok), timeout=15).json()
    assert d["status"] == "running"
    assert d["openai_base_url"] == f"/v1/{DEPLOY_NAME}"


def test_deploy_error_empty_when_running(admin_tok):
    d = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                     headers=H(admin_tok), timeout=15).json()
    assert d["error"] == "", f"运行中部署不应有陈旧 error: {d['error']}"


def test_create_bad_engine_400(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500d1-{SUF}",
                            "model_asset_id": 1, "engine": "bogus"})
    assert r.status_code == 400


def test_create_bad_asset_400(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500d2-{SUF}",
                            "model_asset_id": 999999})
    assert r.status_code == 400


def test_create_duplicate_name_400(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15,
                      json={"project_id": DEFAULT_PROJECT, "name": DEPLOY_NAME,
                            "model_asset_id": 1})
    assert r.status_code == 400


def test_create_missing_422(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15, json={"name": "x"})
    assert r.status_code == 422


def test_preview_launch_config(admin_tok):
    """启动配置预览（GPUWorks 式部署页）：与 build_cmd 同一命令拼装路径，
    不建部署/不占卡。协同=单副本命令+env+真实模型路径；PD=prefill/decode
    双角色命令（KV 传输参数在位）；无模型=占位符；未知后端 400。"""
    base = {
        "name": "preview-demo", "model_asset_id": BASE_MODEL_ASSET_ID,
        "engine": "vllm", "tensor_parallel": 2, "replicas": 1,
        "max_model_len": 8192, "gpu_mem_util": 0.85,
        "advanced": {"max_num_seqs": 32},
    }
    r = requests.post(f"{BASE}/api/inference/deployments/preview",
                      headers=H(admin_tok), timeout=15, json=base)
    r.raise_for_status()
    d = r.json()
    assert d["image"].startswith("vllm/")
    assert d["model_path"] and "<model_path>" not in d["model_path"]
    assert "VLLM_ALLOW_RUNTIME_LORA_UPDATING" in d["env"]
    assert len(d["commands"]) == 1
    c0 = d["commands"][0]
    assert c0["label"] == "replica r0" and c0["gpus"] == 2
    assert "vllm serve" in c0["cmd"] and "--max-model-len 8192" in c0["cmd"]
    assert "--max-num-seqs 32" in c0["cmd"]
    assert "--tensor-parallel-size 2" in c0["cmd"]

    # PD 分离：prefill/decode 双角色命令 + KV 传输参数（mooncake）
    r2 = requests.post(f"{BASE}/api/inference/deployments/preview",
                       headers=H(admin_tok), timeout=15,
                       json={**base, "pd_mode": "pd", "kv_connector": "mooncake",
                             "prefill_replicas": 2, "prefill_tp": 1,
                             "decode_replicas": 1, "decode_tp": 2})
    r2.raise_for_status()
    d2 = r2.json()
    assert [c["label"] for c in d2["commands"]] == ["prefill p0", "decode d0"]
    p, dd = d2["commands"]
    assert p["gpus"] == 1 and dd["gpus"] == 2
    assert "kv_producer" in p["cmd"] and "MooncakeConnector" in p["cmd"]
    assert "kv_consumer" in dd["cmd"]

    # 无模型资产 → 占位符路径；SGLang argv 形态
    r4 = requests.post(f"{BASE}/api/inference/deployments/preview",
                       headers=H(admin_tok), timeout=15,
                       json={"engine": "sglang", "max_model_len": 4096})
    r4.raise_for_status()
    d4 = r4.json()
    assert "<model_path>" in d4["commands"][0]["cmd"]
    assert "sglang.launch_server" in d4["commands"][0]["cmd"]

    # 未知自定义后端 → 400
    r3 = requests.post(f"{BASE}/api/inference/deployments/preview",
                       headers=H(admin_tok), timeout=15,
                       json={**base, "backend_name": "nope-custom"})
    assert r3.status_code == 400


def test_logs_200(admin_tok):
    r = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/logs",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert "logs" in r.json()


def test_logs_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/inference/deployments/999999/logs",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_start_missing_404(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments/999999/start",
                      headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


# ---------- 多 LoRA 热加载 ----------

def test_adapter_add_missing_400(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/adapters",
                      headers=H(admin_tok), timeout=30, json={})
    assert r.status_code == 400


def test_adapter_add_real(admin_tok):
    a = _lora_asset(admin_tok)
    if not a:
        pytest.skip("无可用 LoRA 资产")
    name = f"500hot-{SUF}"
    r = requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/adapters",
                      headers=H(admin_tok), timeout=300,
                      json={"name": name, "asset_id": a["id"]})
    assert r.status_code == 200, r.text[:300]
    assert r.json().get("ok"), f"热加载失败: {r.text[:300]}"
    # 部署记录了 adapter
    d = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                     headers=H(admin_tok), timeout=15).json()
    assert any(x.get("name") == name for x in d["lora_adapters"])
    # 注：vLLM 0.15.1 无 GET /v1/loras 列表端点（仅 /v1/load|unload_lora_adapter），
    # 以部署记录 lora_adapters 含该 adapter 为准


def test_adapter_remove_real(admin_tok):
    a = _lora_asset(admin_tok)
    if not a:
        pytest.skip("无可用 LoRA 资产")
    name = f"500hot-{SUF}"
    r = requests.delete(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/adapters/{name}",
                        headers=H(admin_tok), timeout=120)
    assert r.status_code == 200
    assert r.json().get("ok"), f"卸载失败: {r.text[:300]}"


def test_adapter_remove_missing_ok_false(admin_tok):
    r = requests.delete(
        f"{BASE}/api/inference/deployments/{DEPLOY_ID}/adapters/no-such-{SUF}",
        headers=H(admin_tok), timeout=60)
    assert r.status_code == 200
    assert r.json().get("ok") is False


# ---------- 弹性扩缩容 ----------

def test_scale_same_replicas(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/scale",
                      headers=H(admin_tok), timeout=60, json={"replicas": 1})
    assert r.status_code == 200


def test_scale_up_no_gpu_400(admin_tok):
    """部署 1 占卡 → 扩容超出本机 GPU 容量应被拒。
    按节点实际卡数自适应（旧版写死"单卡节点→扩 2 副本"，双卡机上物理可行 → 200 误报）"""
    r = requests.get(f"{BASE}/api/cluster/nodes", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200, r.text
    online = [n for n in r.json() if n.get("status") == "online"]
    local = next((n for n in online if n["name"] == "node-1"),
                 (max(online, key=lambda n: n.get("gpu_count") or 0) if online
                  else {"gpu_count": 1}))
    n_gpu = local.get("gpu_count") or 1
    r = requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/scale",
                      headers=H(admin_tok), timeout=60, json={"replicas": n_gpu + 1})
    assert r.status_code == 400, f"GPU 不足应 400: {r.status_code} {r.text[:200]}"


def test_scale_missing_404(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments/999999/scale",
                      headers=H(admin_tok), timeout=30, json={"replicas": 1})
    assert r.status_code == 404


# ---------- 高级参数真实部署（唯一一次真实容器，验证命令被 vLLM 接受） ----------

ADV_NAME = f"adv500-{SUF}"


def _wait_terminal(admin_tok, did, timeout=900, interval=5):
    """轮询部署直到终态（running/failed/error），返回其明细"""
    t0 = time.time()
    last = {}
    while time.time() - t0 < timeout:
        r = requests.get(f"{BASE}/api/inference/deployments/{did}",
                         headers=H(admin_tok), timeout=15)
        last = r.json() if r.status_code == 200 else {}
        if last.get("status") in ("running", "failed", "error"):
            return last
        time.sleep(interval)
    return last


# ---------- 部署放置 / 运行环境（目标节点 / 标签约束 / GPU 选择 / PP / 镜像 / Docker 参数 / 命令编辑） ----------

def test_brief_placement_fields(admin_tok):
    """_brief 回显放置/运行环境新字段（存量部署取默认值）"""
    d = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                     headers=H(admin_tok), timeout=15).json()
    for k in ("node_name", "node_labels", "gpu_ids", "pipeline_parallel",
              "image_ref", "docker_args", "cmd_override"):
        assert k in d, f"缺字段 {k}"
    assert d["pipeline_parallel"] >= 1


def test_preview_placement_pp(admin_tok):
    """PP：vllm 命令含 --pipeline-parallel-size，gpus = TP×PP；PD 两角色同样 ×PP"""
    r = requests.post(f"{BASE}/api/inference/deployments/preview",
                      headers=H(admin_tok), timeout=15,
                      json={"name": "pp-demo", "model_asset_id": BASE_MODEL_ASSET_ID,
                            "engine": "vllm", "tensor_parallel": 1, "replicas": 1,
                            "pipeline_parallel": 2})
    r.raise_for_status()
    c0 = r.json()["commands"][0]
    assert c0["gpus"] == 2, f"每实例卡数应为 TP×PP=2: {c0}"
    assert "--pipeline-parallel-size 2" in c0["cmd"]
    r2 = requests.post(f"{BASE}/api/inference/deployments/preview",
                       headers=H(admin_tok), timeout=15,
                       json={"name": "pp-demo", "model_asset_id": BASE_MODEL_ASSET_ID,
                             "engine": "vllm", "pd_mode": "pd", "pipeline_parallel": 2,
                             "prefill_tp": 1, "decode_tp": 1})
    r2.raise_for_status()
    cmds = r2.json()["commands"]
    assert cmds[0]["gpus"] == 2 and cmds[1]["gpus"] == 2, f"PD 角色卡数应 ×PP: {cmds}"


def test_preview_cmd_override(admin_tok):
    """编辑命令文本：预览命令 == 用户编辑内容（bash -lc 原样执行）"""
    r = requests.post(f"{BASE}/api/inference/deployments/preview",
                      headers=H(admin_tok), timeout=15,
                      json={"name": "ov-demo", "model_asset_id": BASE_MODEL_ASSET_ID,
                            "engine": "vllm",
                            "cmd_override": "echo hi && vllm serve /models/x"})
    r.raise_for_status()
    assert r.json()["commands"][0]["cmd"] == "echo hi && vllm serve /models/x"


def test_preview_image_ref(admin_tok):
    """自定义镜像引用：最高优先级，覆盖系统 pin 镜像"""
    r = requests.post(f"{BASE}/api/inference/deployments/preview",
                      headers=H(admin_tok), timeout=15,
                      json={"name": "img-demo", "model_asset_id": BASE_MODEL_ASSET_ID,
                            "engine": "vllm",
                            "image_ref": "registry.local:5000/vervedeck/vllm:custom"})
    r.raise_for_status()
    assert r.json()["image"] == "registry.local:5000/vervedeck/vllm:custom"


def test_engine_versions_endpoint(admin_tok):
    """引擎版本列表端点：当前生效 tag + 可用 tag（拉取网络失败时 versions 为空但不报错）"""
    for eng in ("vllm", "sglang"):
        r = requests.get(f"{BASE}/api/inference/engine-versions",
                         headers=H(admin_tok), timeout=60, params={"engine": eng})
        r.raise_for_status()
        d = r.json()
        assert d["engine"] == eng
        assert d["current"], "平台当前版本 tag 不应为空"
        assert isinstance(d["versions"], list)
    r2 = requests.get(f"{BASE}/api/inference/engine-versions", headers=H(admin_tok),
                      timeout=15, params={"engine": "llamafactory"})
    assert r2.status_code == 400


def test_preview_engine_version(admin_tok):
    """引擎版本：builtin 引擎指定 tag → 镜像 = hub:tag；非法格式 400"""
    r = requests.post(f"{BASE}/api/inference/deployments/preview",
                      headers=H(admin_tok), timeout=30,
                      json={"name": "ev-demo", "model_asset_id": BASE_MODEL_ASSET_ID,
                            "engine": "vllm", "engine_version": "v0.33.0"})
    r.raise_for_status()
    assert r.json()["image"] == "vllm/vllm-openai:v0.33.0"
    r2 = requests.post(f"{BASE}/api/inference/deployments/preview",
                       headers=H(admin_tok), timeout=15,
                       json={"name": "ev-bad", "model_asset_id": BASE_MODEL_ASSET_ID,
                             "engine": "vllm", "engine_version": "not-a-version"})
    assert r2.status_code == 400


def test_preview_engine_version_pd_min(admin_tok):
    """PD 分离：引擎版本低于参数面下限（vLLM <0.30）→ 400"""
    r = requests.post(f"{BASE}/api/inference/deployments/preview",
                      headers=H(admin_tok), timeout=15,
                      json={"name": "ev-pd", "model_asset_id": BASE_MODEL_ASSET_ID,
                            "engine": "vllm", "pd_mode": "pd", "engine_version": "v0.25.0"})
    assert r.status_code == 400


def test_preview_tp_auto_and_node(admin_tok):
    """TP 自动（0）：返回 tp_auto 估算结果；node 返回节点名/卡数/空闲/标签。
    PD 模式不支持 TP 自动 → 400。"""
    r = requests.post(f"{BASE}/api/inference/deployments/preview",
                      headers=H(admin_tok), timeout=15,
                      json={"name": "tp-auto-demo", "model_asset_id": BASE_MODEL_ASSET_ID,
                            "engine": "vllm", "tensor_parallel": 0})
    r.raise_for_status()
    d = r.json()
    assert d["tp_auto"] and d["tp_auto"]["value"] >= 1 and d["tp_auto"]["reason"]
    assert d["node"] and d["node"]["name"] and d["node"]["total"] >= 0
    assert "labels" in d["node"] and "free" in d["node"]
    r2 = requests.post(f"{BASE}/api/inference/deployments/preview",
                       headers=H(admin_tok), timeout=15,
                       json={"name": "tp-auto-pd", "engine": "vllm",
                             "pd_mode": "pd", "tensor_parallel": 0})
    assert r2.status_code == 400


def _plc_body(**kw):
    b = {"project_id": DEFAULT_PROJECT, "name": f"500plc-{SUF}",
         "model_asset_id": BASE_MODEL_ASSET_ID, "engine": "vllm",
         "tensor_parallel": 1, "replicas": 1}
    b.update(kw)
    return b


def test_create_gpu_ids_mismatch_400(admin_tok):
    """手动 GPU：数量须 = TP×PP×副本"""
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15, json=_plc_body(gpu_ids=[0, 1]))
    assert r.status_code == 400


def test_create_gpu_ids_range_400(admin_tok):
    """手动 GPU：卡号超出节点卡数范围"""
    nodes = requests.get(f"{BASE}/api/cluster/nodes",
                         headers=H(admin_tok), timeout=15).json()
    local = next((n for n in nodes if n.get("local")), {"gpu_count": 2})
    out = (local.get("gpu_count") or 2) + 10
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15, json=_plc_body(gpu_ids=[out]))
    assert r.status_code == 400


def test_create_gpu_ids_dup_400(admin_tok):
    """手动 GPU：卡号重复"""
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15,
                      json=_plc_body(tensor_parallel=2, gpu_ids=[0, 0]))
    assert r.status_code == 400


def test_create_pd_gpu_ids_400(admin_tok):
    """PD 分离不支持手动 GPU 选择"""
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15, json=_plc_body(pd_mode="pd", gpu_ids=[0]))
    assert r.status_code == 400


def test_create_node_name_remote_400(admin_tok):
    """目标节点：非本节点不可调度（当前单节点可调度）"""
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15, json=_plc_body(node_name="remote-no-such"))
    assert r.status_code == 400


def test_create_label_missing_400(admin_tok):
    """节点标签约束：本节点缺标签 → 400"""
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15,
                      json=_plc_body(node_labels=["zz-no-such-label=1"]))
    assert r.status_code == 400


def test_create_bad_image_ref_400(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15, json=_plc_body(image_ref="no-slash-at-all!!"))
    assert r.status_code == 400


def test_create_bad_docker_args_400(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15, json=_plc_body(docker_args=["--no-such-flag=1"]))
    assert r.status_code == 400


def test_create_bad_engine_version_400(admin_tok):
    """引擎版本：非完整 semver tag（如仅 0.33）→ 400"""
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15, json=_plc_body(engine_version="0.33"))
    assert r.status_code == 400


def test_update_bad_engine_version_400(admin_tok):
    """编辑：非法引擎版本 → 400（校验前置，不触发实例重建）"""
    lst = requests.get(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                       timeout=15, params={"project_id": DEFAULT_PROJECT}).json()
    assert lst, "至少应有一个部署"
    did = lst[0]["id"]
    r = requests.put(f"{BASE}/api/inference/deployments/{did}", headers=H(admin_tok),
                     timeout=15, json={"engine_version": "bad-tag"})
    assert r.status_code == 400


def test_create_pd_cmd_override_400(admin_tok):
    """PD 分离不支持编辑命令文本"""
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15,
                      json=_plc_body(pd_mode="pd", cmd_override="echo hi"))
    assert r.status_code == 400


def test_create_pd_tp_auto_400(admin_tok):
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=15, json=_plc_body(pd_mode="pd", tensor_parallel=0))
    assert r.status_code == 400


def test_update_placement_400s(admin_tok):
    """update 放置新字段走同口径校验：校验失败 400（不触发重建，常驻部署不受影响）"""
    bodies = [{"node_name": "remote-no-such"},
              {"node_labels": ["zz-no-such-label=1"]},
              {"docker_args": ["--no-such-flag=1"]},
              {"image_ref": "no-slash-at-all!!"}]
    # gpu_ids 数量不符：按当前 gpu_need 多构造 1 张（协同模式才校验卡号数量）
    d0 = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                      headers=H(admin_tok), timeout=15).json()
    if d0.get("pd_mode", "off") != "pd":
        bodies.append({"gpu_ids": list(range(d0["gpu_need"] + 1))})
    for body in bodies:
        r = requests.put(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                         headers=H(admin_tok), timeout=15, json=body)
        assert r.status_code == 400, f"{body} 应 400: {r.status_code} {r.text[:200]}"
    # 常驻部署未被误改
    d = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                     headers=H(admin_tok), timeout=15).json()
    assert d["image_ref"] == "" and d["cmd_override"] == "" and d["docker_args"] == []


PLC_NAME = f"plc500-{SUF}"


def test_update_placement_fields_real(admin_tok):
    """update 放置/运行环境新字段 200 路径：轻量部署 → 命令编辑/自定义镜像/Docker
    参数回显 + 清空语义（""/[] 恢复自动）→ stop+delete 清理。不等终态避免长时间占卡。
    PP 提 2 需 1 张增量卡：仅在本机有空闲卡时验证（双卡机常驻部署占 1 卡时跳过该项）。"""
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=30, json=_plc_body(name=PLC_NAME))
    r.raise_for_status()
    did = r.json()["id"]
    try:
        nodes = requests.get(f"{BASE}/api/cluster/nodes",
                             headers=H(admin_tok), timeout=15).json()
        local = next((n for n in nodes if n.get("local")), {})
        free = (local.get("gpu_count") or 0) - (local.get("gpu_in_use") or 0)
        put_body = {"cmd_override": "echo plc-test",
                    "image_ref": "registry.local:5000/vervedeck/vllm:custom",
                    "docker_args": ["--shm-size=2g"]}
        want_pp = 2 if free >= 1 else 1
        if want_pp > 1:
            put_body["pipeline_parallel"] = 2
        r = requests.put(f"{BASE}/api/inference/deployments/{did}",
                         headers=H(admin_tok), timeout=30, json=put_body)
        r.raise_for_status()
        d = r.json()
        assert d["pipeline_parallel"] == want_pp
        assert d["gpu_need"] == want_pp, f"gpu_need=tp1×pp{want_pp}×rep1: {d['gpu_need']}"
        assert d["cmd_override"] == "echo plc-test"
        assert d["image_ref"] == "registry.local:5000/vervedeck/vllm:custom"
        assert d["docker_args"] == ["--shm-size=2g"]
        # 清空语义：空串/[] 恢复自动拼装/自动镜像/无参数
        r = requests.put(f"{BASE}/api/inference/deployments/{did}",
                         headers=H(admin_tok), timeout=30,
                         json={"cmd_override": "", "image_ref": "", "docker_args": []})
        r.raise_for_status()
        d = r.json()
        assert d["cmd_override"] == "" and d["image_ref"] == "" and d["docker_args"] == []
        time.sleep(5)  # 给重建线程留出建容器窗口，避免清理竞态
    finally:
        try:
            requests.post(f"{BASE}/api/inference/deployments/{did}/stop",
                          headers=H(admin_tok), timeout=120)
        except Exception:
            pass
        requests.delete(f"{BASE}/api/inference/deployments/{did}",
                        headers=H(admin_tok), timeout=60)


def test_node_labels_set_read_clear(admin_tok):
    """节点标签：设置 → 回读 → 标签约束 preview 联动（满足 200 / 缺失 400）→ 恢复原值；
    非 key=value 格式 400。"""
    nodes = requests.get(f"{BASE}/api/cluster/nodes",
                         headers=H(admin_tok), timeout=15).json()
    node = next((n for n in nodes if n.get("local")), None)
    if not node:
        pytest.skip("无本节点")
    assert "labels" in node, "nodes 响应应含 labels 字段"
    orig = node.get("labels") or []
    nid = node["id"]
    try:
        r = requests.put(f"{BASE}/api/cluster/nodes/{nid}", headers=H(admin_tok),
                         timeout=15, json={"labels": ["env=test", "gpu=4090"]})
        r.raise_for_status()
        assert r.json().get("labels") == ["env=test", "gpu=4090"]
        # 标签约束满足 → preview 200；缺失 → 400
        rp = requests.post(f"{BASE}/api/inference/deployments/preview",
                           headers=H(admin_tok), timeout=15,
                           json={"name": "lbl-demo", "node_labels": ["env=test"]})
        assert rp.status_code == 200, rp.text[:200]
        rp2 = requests.post(f"{BASE}/api/inference/deployments/preview",
                            headers=H(admin_tok), timeout=15,
                            json={"name": "lbl-demo", "node_labels": ["env=prod"]})
        assert rp2.status_code == 400
    finally:
        requests.put(f"{BASE}/api/cluster/nodes/{nid}", headers=H(admin_tok),
                     timeout=15, json={"labels": orig})
    # 非 key=value 格式拒绝
    r = requests.put(f"{BASE}/api/cluster/nodes/{nid}", headers=H(admin_tok),
                     timeout=15, json={"labels": ["bad-label-no-eq"]})
    assert r.status_code == 400


def test_deploy_advanced_params_real(admin_tok):
    """带 advanced 参数真实部署 vLLM：单测只验证命令字符串正确，本用例验证
    vLLM 真实接受这些参数（dtype/enforce_eager/max_num_seqs）启动并能提供
    推理服务。在空闲卡上与常驻部署并行，finally 内 stop+delete 清理不占卡。

    若某高级参数被 vLLM 版本拒绝（如不认 flag / 与量化冲突），部署会 failed，
    本用例以失败暴露——这正是单测覆盖不到的"参数进真实容器"链路。
    """
    body = {"project_id": DEFAULT_PROJECT, "name": ADV_NAME,
            "model_asset_id": BASE_MODEL_ASSET_ID, "engine": "vllm",
            "tensor_parallel": 1, "max_model_len": 4096,
            "advanced": {"dtype": "bfloat16", "enforce_eager": True,
                         "max_num_seqs": 32},
            "replicas": 1}
    r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                      timeout=30, json=body)
    r.raise_for_status()
    did = r.json()["id"]
    try:
        d = _wait_terminal(admin_tok, did, timeout=900)
        assert d.get("status") == "running", \
            (f"带 advanced 参数的部署应成功启动: status={d.get('status')} "
             f"error={str(d.get('error'))[:300]}")
        r = _chat(tok=admin_tok, name=ADV_NAME, content="只回复两个字: 好的")
        assert r.status_code == 200, \
            f"advanced 部署代理 chat 应成功: {r.status_code} {r.text[:200]}"
        assert r.json().get("choices"), "chat 响应应含 choices"
    finally:
        try:
            requests.post(f"{BASE}/api/inference/deployments/{did}/stop",
                          headers=H(admin_tok), timeout=120)
        except Exception:
            pass
        requests.delete(f"{BASE}/api/inference/deployments/{did}",
                        headers=H(admin_tok), timeout=60)

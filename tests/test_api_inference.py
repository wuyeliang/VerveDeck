# -*- coding: utf-8 -*-
"""推理部署 API：部署管理 / OpenAI 兼容代理（鉴权×护栏×流式）/ 多 LoRA 热加载 / 扩缩容

依赖常驻部署 1（qwen25-05b-base, running）。不创建新部署（GPU 单卡互斥）。
"""
import pytest
import requests

from conftest import BASE, H, SUF, DEFAULT_PROJECT, DEPLOY_ID, DEPLOY_NAME


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

# -*- coding: utf-8 -*-
"""推理部署生命周期（唯一一次真实部署切换）：

停部署 1 → 503 验证 → 新建部署 2（真实 vLLM 启动）→ 代理验证 → LoRA 热加载
→ 扩容校验 → 停止/删除部署 2 → 恢复部署 1 running

module fixture 的 teardown 无条件兜底：删除残留部署 2 + 恢复部署 1。
本文件按字母序先于 test_api_evals / test_api_inference 执行。
"""
import os
import time

import pytest
import requests

from conftest import BASE, H, SUF, poll, DEFAULT_PROJECT, DEPLOY_ID, DEPLOY_NAME


def _dep1(admin_tok):
    r = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                     headers=H(admin_tok), timeout=15)
    return r.json() if r.status_code == 200 else {}


def _wait_status(admin_tok, did, want, timeout=1200, desc=""):
    def check():
        r = requests.get(f"{BASE}/api/inference/deployments/{did}",
                         headers=H(admin_tok), timeout=15)
        d = r.json() if r.status_code == 200 else {}
        return d if d.get("status") == want else d
    return poll(check, timeout=timeout, interval=5, desc=desc or f"部署 {did} -> {want}")


def _wait_proxy_ok(admin_tok, name, timeout=180, desc=""):
    def check():
        r = requests.get(f"{BASE}/v1/{name}/models", headers=H(admin_tok), timeout=8)
        return r if r.status_code == 200 else None
    return poll(check, timeout=timeout, interval=5, desc=desc or f"代理 {name} 就绪")


DEP2_NAME = f"lc500-{SUF}"


@pytest.fixture(scope="module")
def lc(admin_tok):
    """停部署 1 → 建部署 2（creating）；teardown 恢复部署 1 running"""
    c = {}
    requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/stop",
                  headers=H(admin_tok), timeout=60).raise_for_status()
    c["dep1_after_stop"] = _wait_status(admin_tok, DEPLOY_ID, "stopped",
                                        timeout=120, desc="部署 1 停止")
    try:
        r = requests.post(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                          timeout=30,
                          json={"project_id": DEFAULT_PROJECT, "name": DEP2_NAME,
                                "model_asset_id": 1, "engine": "vllm",
                                "tensor_parallel": 1, "max_model_len": 4096,
                                "gpu_mem_util": 0.8, "enable_lora": True,
                                "replicas": 1})
        r.raise_for_status()
        c["dep2_id"] = r.json()["id"]
    except Exception:
        _restore_dep1(admin_tok)
        raise
    yield c
    # ---- teardown 兜底 ----
    try:
        r = requests.get(f"{BASE}/api/inference/deployments/{c.get('dep2_id', 0)}",
                         headers=H(admin_tok), timeout=15)
        if r.status_code == 200:
            d = r.json()
            if d["status"] in ("running", "creating"):
                requests.post(f"{BASE}/api/inference/deployments/{c['dep2_id']}/stop",
                              headers=H(admin_tok), timeout=120)
            requests.delete(f"{BASE}/api/inference/deployments/{c['dep2_id']}",
                            headers=H(admin_tok), timeout=60)
    except Exception:
        pass
    _restore_dep1(admin_tok)


def _restore_dep1(admin_tok):
    d = _dep1(admin_tok)
    if d.get("status") != "running":
        r = requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/start",
                          headers=H(admin_tok), timeout=30)
        if r.status_code == 200:
            poll(lambda: _dep1(admin_tok).get("status") == "running" or None,
                 timeout=1800, interval=5, desc="恢复部署 1 running")


# ---------- 用例（按文件内顺序执行） ----------

def test_deploy1_stopped_proxy_503(admin_tok, lc):
    """部署 1 停止后，其代理端点应 503"""
    r = requests.get(f"{BASE}/v1/{DEPLOY_NAME}/models", headers=H(admin_tok), timeout=15)
    assert r.status_code == 503, f"停止的部署代理应 503: {r.status_code} {r.text[:200]}"
    assert lc["dep1_after_stop"]["status"] == "stopped"


def test_new_deploy_reaches_running(admin_tok, lc):
    """部署 2 真实拉起 vLLM 直至 running（0.5B 模型，<10min）"""
    d = _wait_status(admin_tok, lc["dep2_id"], "running", timeout=1200,
                     desc="部署 2 启动")
    assert d.get("error") == "", f"部署 2 有错误: {d.get('error')}"
    _wait_proxy_ok(admin_tok, DEP2_NAME, timeout=300, desc="部署 2 代理就绪")


def test_new_deploy_proxy_chat(admin_tok, lc):
    """部署 2 的 OpenAI 兼容代理可用"""
    r = requests.get(f"{BASE}/v1/{DEP2_NAME}/models", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    names = [m["id"] for m in r.json().get("data", [])]
    assert DEP2_NAME in names
    r = requests.post(f"{BASE}/v1/{DEP2_NAME}/chat/completions", headers=H(admin_tok),
                      timeout=120,
                      json={"model": DEP2_NAME,
                            "messages": [{"role": "user", "content": "只回复两个字母: OK"}],
                            "max_tokens": 16, "temperature": 0})
    assert r.status_code == 200, r.text[:300]
    assert r.json()["choices"][0]["message"]["content"].strip()


def test_new_deploy_lora_hotload(admin_tok, lc):
    """部署 2 运行期热加载真实 LoRA（enable_lora=True 启动）"""
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT, "mtype": "lora"},
                        headers=H(admin_tok), timeout=15).json()
    with_job = [a for a in rows if a.get("job_id") and os.path.isdir(a["path"])]
    if not with_job:
        pytest.skip("无真实 LoRA 检查点")
    a = with_job[0]
    name = f"lc500hot-{SUF}"
    r = requests.post(f"{BASE}/api/inference/deployments/{lc['dep2_id']}/adapters",
                      headers=H(admin_tok), timeout=300,
                      json={"name": name, "asset_id": a["id"]})
    assert r.status_code == 200, r.text[:300]
    assert r.json().get("ok"), f"热加载失败: {r.text[:300]}"
    d = _get2(admin_tok, lc)
    assert any(x.get("name") == name for x in d["lora_adapters"])
    # 卸载
    r = requests.delete(f"{BASE}/api/inference/deployments/{lc['dep2_id']}/adapters/{name}",
                        headers=H(admin_tok), timeout=120)
    assert r.status_code == 200 and r.json().get("ok"), r.text[:300]


def _get2(admin_tok, lc):
    return requests.get(f"{BASE}/api/inference/deployments/{lc['dep2_id']}",
                        headers=H(admin_tok), timeout=15).json()


def test_scale_up_no_gpu_then_stop_delete(admin_tok, lc):
    """扩容超出本机 GPU 容量应被拒（400）；随后停止并删除部署 2。
    按节点实际卡数自适应：部署 2 占 1 卡（fixture 已停部署 1），扩到
    gpu_count+1 副本必超出本机容量。（旧版写死"单卡节点→扩 2 副本"，
    双卡机上扩容物理可行 → 200 误报。）"""
    r = requests.get(f"{BASE}/api/cluster/nodes", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200, r.text
    online = [n for n in r.json() if n.get("status") == "online"]
    local = next((n for n in online if n["name"] == "node-1"),
                 (max(online, key=lambda n: n.get("gpu_count") or 0) if online
                  else {"gpu_count": 1}))
    n_gpu = local.get("gpu_count") or 1
    r = requests.post(f"{BASE}/api/inference/deployments/{lc['dep2_id']}/scale",
                      headers=H(admin_tok), timeout=60, json={"replicas": n_gpu + 1})
    assert r.status_code == 400, f"超出 GPU 容量扩容应 400: {r.status_code} {r.text[:200]}"
    # 停止
    r = requests.post(f"{BASE}/api/inference/deployments/{lc['dep2_id']}/stop",
                      headers=H(admin_tok), timeout=120)
    assert r.status_code == 200
    d = _wait_status(admin_tok, lc["dep2_id"], "stopped", timeout=120,
                     desc="部署 2 停止")
    assert d["status"] == "stopped"
    # 删除
    r = requests.delete(f"{BASE}/api/inference/deployments/{lc['dep2_id']}",
                        headers=H(admin_tok), timeout=60)
    assert r.status_code == 200
    r = requests.get(f"{BASE}/api/inference/deployments/{lc['dep2_id']}",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_restore_deploy1_running(admin_tok, lc):
    """恢复部署 1：start → running → 代理可用 → error 清空"""
    d = _dep1(admin_tok)
    if d.get("status") != "running":
        r = requests.post(f"{BASE}/api/inference/deployments/{DEPLOY_ID}/start",
                          headers=H(admin_tok), timeout=30)
        assert r.status_code == 200, r.text
    _wait_status(admin_tok, DEPLOY_ID, "running", timeout=1800, desc="部署 1 恢复")
    _wait_proxy_ok(admin_tok, DEPLOY_NAME, timeout=300, desc="部署 1 代理恢复")
    d = _dep1(admin_tok)
    assert d["status"] == "running"
    assert d["error"] == "", f"恢复后 error 应清空: {d['error']}"

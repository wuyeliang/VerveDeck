# -*- coding: utf-8 -*-
"""推理后端注册表：builtin 种子 / custom CRUD / 版本管理 / YAML 导出-导入回环。

不消耗 GPU（纯注册表数据面）。实体带 SUF 后缀，teardown 清理。
"""
import requests
import yaml

from conftest import BASE, H, SUF

BNAME = f"tb{SUF}-custom"


def _list(admin_tok):
    r = requests.get(f"{BASE}/api/inference/backends", headers=H(admin_tok), timeout=15)
    r.raise_for_status()
    return r.json()


def test_list_contains_builtins(admin_tok):
    """builtin 种子（vllm/sglang）存在且带基线版本"""
    rows = {b["name"]: b for b in _list(admin_tok)}
    assert "vllm" in rows and rows["vllm"]["source"] == "builtin"
    assert "sglang" in rows and rows["sglang"]["source"] == "builtin"
    assert rows["vllm"]["versions"], "builtin vllm 应有基线版本"
    assert rows["vllm"]["current_image"], "builtin vllm 应有生效镜像（pin 系统）"


def test_builtins_undeletable(admin_tok):
    rows = {b["name"]: b for b in _list(admin_tok)}
    r = requests.delete(f"{BASE}/api/inference/backends/{rows['vllm']['id']}",
                        headers=H(admin_tok), timeout=15)
    assert r.status_code == 403, f"内置后端应不可删: {r.status_code} {r.text[:200]}"


def _make_custom(admin_tok) -> dict:
    r = requests.post(f"{BASE}/api/inference/backends", headers=H(admin_tok), timeout=15,
                      json={"name": BNAME, "description": "auto-test backend",
                            "health_check_path": "/health",
                            "default_env": {"K": "v"},
                            "default_params": {"max_model_len": 4096}})
    r.raise_for_status()
    b = r.json()
    for ver, img, fw in (("v1.0", "lm/test:v1.0", "cuda"), ("v0.9", "lm/test:v0.9", "rocm")):
        r = requests.post(f"{BASE}/api/inference/backends/{b['id']}/versions",
                          headers=H(admin_tok), timeout=15,
                          json={"version": ver, "image": img, "framework": fw,
                                "entrypoint": "/bin/sh -c" if ver == "v1.0" else "",
                                "run_command": "python3 -m x --model {{model_path}} --port {{port}}",
                                "env": {"EXTRA": "1"},
                                "is_default": ver == "v1.0"})
        r.raise_for_status()
        b = r.json()
    return b


def test_custom_lifecycle_and_yaml_roundtrip(admin_tok):
    """custom 创建 → 版本/默认断言 → 导出 YAML（import 同格式）→ 删除 → 导入重建 → 清理"""
    b = _make_custom(admin_tok)
    bid = b["id"]
    try:
        assert b["source"] == "custom"
        vers = {v["version"]: v for v in b["versions"]}
        assert set(vers) == {"v1.0", "v0.9"}
        assert vers["v1.0"]["is_default"] and not vers["v0.9"]["is_default"]
        assert b["current_image"] == "lm/test:v1.0", "custom 生效镜像=默认版本镜像"

        # 导出 YAML：与 import 同格式（backends: [{name, versions: [...]}]）
        r = requests.get(f"{BASE}/api/inference/backends/{bid}/export",
                         headers=H(admin_tok), timeout=15)
        assert r.status_code == 200, r.text
        assert "yaml" in (r.headers.get("content-type") or "").lower()
        data = yaml.safe_load(r.text)
        items = data["backends"] if isinstance(data, dict) and "backends" in data else data
        assert isinstance(items, list) and len(items) == 1
        item = items[0]
        assert item["name"] == BNAME
        assert item["health_check_path"] == "/health"
        assert item["default_params"] == {"max_model_len": 4096}
        vmap = {v["version"]: v for v in item["versions"]}
        assert set(vmap) == {"v1.0", "v0.9"}
        assert vmap["v1.0"]["is_default"] and vmap["v0.9"]["framework"] == "rocm"
        yaml_text = r.text
    finally:
        requests.delete(f"{BASE}/api/inference/backends/{bid}",
                        headers=H(admin_tok), timeout=30)

    # 导入回环：删除后用导出的 YAML 重建（导入仅创建、不覆盖）
    fd = {"file": (f"{BNAME}.yaml", yaml_text.encode("utf-8"), "application/x-yaml")}
    r = requests.post(f"{BASE}/api/inference/backends/import", headers=H(admin_tok),
                      files=fd, timeout=30)
    assert r.status_code == 200, f"导出 YAML 应可回导: {r.status_code} {r.text[:300]}"
    again = next(x for x in _list(admin_tok) if x["name"] == BNAME)
    assert {v["version"] for v in again["versions"]} == {"v1.0", "v0.9"}
    assert again["current_image"] == "lm/test:v1.0", "回导后默认版本应保持"
    requests.delete(f"{BASE}/api/inference/backends/{again['id']}",
                    headers=H(admin_tok), timeout=30)


def test_version_delete_guard(admin_tok):
    """最后一个版本不可删（被部署引用 409 需真实部署，属 GPU 套件范畴，此处不覆盖）"""
    b = _make_custom(admin_tok)
    bid = b["id"]
    v0 = next(v for v in b["versions"] if v["version"] == "v0.9")
    v1 = next(v for v in b["versions"] if v["version"] == "v1.0")
    try:
        # 删掉 v0.9 后仅剩 1 个版本，再删 v1.0 应 400
        r = requests.delete(f"{BASE}/api/inference/backends/{bid}/versions/{v0['id']}",
                            headers=H(admin_tok), timeout=15)
        assert r.status_code == 200
        r = requests.delete(f"{BASE}/api/inference/backends/{bid}/versions/{v1['id']}",
                            headers=H(admin_tok), timeout=15)
        assert r.status_code == 400, f"最后一个版本应不可删: {r.status_code} {r.text[:200]}"
    finally:
        requests.delete(f"{BASE}/api/inference/backends/{bid}",
                        headers=H(admin_tok), timeout=30)

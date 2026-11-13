# -*- coding: utf-8 -*-
"""模型资产仓库 API：基座纳管 / 产物注册 / 血缘 / 删除"""
import os

import pytest
import requests

from conftest import BASE, H, SUF, DEFAULT_PROJECT, BASE_MODEL_ASSET_ID


@pytest.fixture(scope="module")
def mctx(admin_tok):
    """默认项目内的测试资产：本地占位基座 + lora 产物(带父) + 无父产物"""
    c = {}
    bname = f"500mbase-{SUF}"
    bpath = f"/data/models/hub/{bname}"
    os.makedirs(bpath, exist_ok=True)
    with open(os.path.join(bpath, "w.txt"), "w") as f:
        f.write("w")
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": bname,
                            "hf_id": "local/placeholder", "note": "500-suite"})
    r.raise_for_status()
    c["base_id"] = r.json()["id"]
    c["base_name"] = bname
    c["base_path"] = bpath

    lname = f"500mlora-{SUF}"
    lpath = f"/data/models/hub/{lname}"
    os.makedirs(lpath, exist_ok=True)
    with open(os.path.join(lpath, "a.txt"), "w") as f:
        f.write("a")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": lname, "type": "lora",
                            "path": lpath, "base_model_name": bname})
    r.raise_for_status()
    c["lora_id"] = r.json()["id"]
    c["lora_name"] = lname
    c["lora_path"] = lpath

    nname = f"500mnoP-{SUF}"
    npath = f"/data/models/hub/{nname}"
    os.makedirs(npath, exist_ok=True)
    with open(os.path.join(npath, "n.txt"), "w") as f:
        f.write("n")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": nname, "type": "lora",
                            "path": npath})
    r.raise_for_status()
    c["nop_id"] = r.json()["id"]
    c["nop_name"] = nname

    # E2E 训练产出的真实 LoRA 资产（存在则供血缘/热加载用）
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT, "mtype": "lora"},
                        headers=H(admin_tok), timeout=15).json()
    e2e = [a for a in rows if a.get("parent_name") and a["parent_name"] != bname
           and a.get("job_id")]
    c["e2e_lora"] = e2e[0] if e2e else None
    return c


def _mk_local(name, content="x"):
    p = f"/data/models/hub/{name}"
    os.makedirs(p, exist_ok=True)
    with open(os.path.join(p, "f.txt"), "w") as f:
        f.write(content)
    return p


# ---------- 列表 ----------

def test_list_200(admin_tok):
    r = requests.get(f"{BASE}/api/models", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200 and isinstance(r.json(), list)


def test_list_project_filter(admin_tok, mctx):
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    assert all(a["project_id"] == DEFAULT_PROJECT for a in rows)
    assert any(a["id"] == mctx["base_id"] for a in rows)


def test_list_type_base(admin_tok, mctx):
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT, "mtype": "base"},
                        headers=H(admin_tok), timeout=15).json()
    assert rows and all(a["type"] == "base" for a in rows)


def test_list_type_lora(admin_tok, mctx):
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT, "mtype": "lora"},
                        headers=H(admin_tok), timeout=15).json()
    assert any(a["id"] == mctx["lora_id"] for a in rows)


def test_list_type_merged(admin_tok):
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT, "mtype": "merged"},
                        headers=H(admin_tok), timeout=15).json()
    assert all(a["type"] == "merged" for a in rows)


def test_list_type_unknown_empty(admin_tok):
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT, "mtype": "bogus"},
                        headers=H(admin_tok), timeout=15).json()
    assert rows == []


def test_list_includes_pending_status(admin_tok):
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    for a in rows:
        assert a["status"] in ("ready", "pending_download", "failed")


def test_list_order_desc(admin_tok):
    rows = requests.get(f"{BASE}/api/models",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    ids = [a["id"] for a in rows]
    assert ids == sorted(ids, reverse=True)


# ---------- 详情 / 字段 ----------

def test_get_base(admin_tok):
    r = requests.get(f"{BASE}/api/models/{BASE_MODEL_ASSET_ID}",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert r.json()["type"] == "base"


def test_get_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/models/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_brief_fields_complete(admin_tok, mctx):
    row = requests.get(f"{BASE}/api/models/{mctx['lora_id']}",
                       headers=H(admin_tok), timeout=15).json()
    for k in ("id", "project_id", "name", "type", "path", "source", "hf_id",
              "size_bytes", "status", "parent_name", "job_id",
              "dataset_version_ids", "note", "created_at"):
        assert k in row, f"缺字段 {k}"


# ---------- 血缘 ----------

def test_lineage_base(admin_tok):
    r = requests.get(f"{BASE}/api/models/{BASE_MODEL_ASSET_ID}/lineage",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    lin = r.json()
    assert lin["asset"]["id"] == BASE_MODEL_ASSET_ID
    assert len(lin["chain"]) == 1


def test_lineage_lora_chain_to_base(admin_tok, mctx):
    lin = requests.get(f"{BASE}/api/models/{mctx['lora_id']}/lineage",
                       headers=H(admin_tok), timeout=15).json()
    chain_ids = [c["id"] for c in lin["chain"]]
    assert chain_ids[-1] == mctx["base_id"], f"血缘链未指向基座: {chain_ids}"
    assert chain_ids[0] == mctx["lora_id"]


def test_lineage_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/models/999999/lineage", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_lineage_children(admin_tok, mctx):
    lin = requests.get(f"{BASE}/api/models/{mctx['base_id']}/lineage",
                       headers=H(admin_tok), timeout=15).json()
    child_ids = [c["id"] for c in lin["children"]]
    assert mctx["lora_id"] in child_ids


def test_lineage_training_jobs_base1(admin_tok):
    """默认项目基座 1 上有 E2E 训练任务 → training_jobs 非空"""
    lin = requests.get(f"{BASE}/api/models/{BASE_MODEL_ASSET_ID}/lineage",
                       headers=H(admin_tok), timeout=15).json()
    assert len(lin["training_jobs"]) >= 1


def test_lineage_dataset_versions_e2e(admin_tok, mctx):
    """E2E 训练产物 LoRA 的血缘应关联训练数据集版本"""
    if not mctx["e2e_lora"]:
        pytest.skip("无 E2E 训练产物 LoRA")
    lin = requests.get(f"{BASE}/api/models/{mctx['e2e_lora']['id']}/lineage",
                       headers=H(admin_tok), timeout=15).json()
    assert len(lin["dataset_versions"]) >= 1


# ---------- 基座注册 ----------

def test_register_base_local_ready(admin_tok):
    name = f"500mrdy-{SUF}"
    _mk_local(name)
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name,
                            "hf_id": "local/x"})
    assert r.status_code == 200
    assert r.json()["status"] == "ready"
    assert r.json()["size_bytes"] > 0


def test_register_base_missing_dir_pending(admin_tok):
    name = f"500mwd-{SUF}"
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name,
                            "hf_id": "org/not-exist-repo"})
    assert r.status_code == 200
    assert r.json()["status"] == "pending_download"


def test_register_base_duplicate_400(admin_tok, mctx):
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": mctx["base_name"],
                            "hf_id": "local/x"})
    assert r.status_code == 400


def test_register_base_missing_hf_422(admin_tok):
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500mnh-{SUF}"})
    assert r.status_code == 422


def test_register_base_note(admin_tok):
    name = f"500mnote-{SUF}"
    _mk_local(name)
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name,
                            "hf_id": "local/x", "note": "my note"})
    assert r.status_code == 200
    assert r.json()["note"] == "my note"


def test_register_base_same_name_other_project_ok(admin_tok, tenant_a, mctx):
    _mk_local(mctx["base_name"] + f"-b-{SUF}")  # 避免同名目录干扰
    r = requests.post(f"{BASE}/api/models/base", headers=H(admin_tok), timeout=30,
                      json={"project_id": tenant_a["proj_id"],
                            "name": mctx["base_name"], "hf_id": "local/x"})
    assert r.status_code == 200, "同名模型跨项目应允许"


# ---------- 产物注册 ----------

def test_register_artifact_lora(admin_tok):
    name = f"500mar-{SUF}"
    p = _mk_local(name, "lora-w")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name, "type": "lora",
                            "path": p})
    assert r.status_code == 200


def test_register_artifact_bad_type_400(admin_tok):
    p = _mk_local(f"500mbt-{SUF}")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500mbt2-{SUF}",
                            "type": "bogus", "path": p})
    assert r.status_code == 400


def test_register_artifact_missing_path_400(admin_tok):
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500mpp-{SUF}",
                            "type": "lora", "path": "/data/no-such-dir-500"})
    assert r.status_code == 400


def test_register_artifact_duplicate_400(admin_tok, mctx):
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": mctx["lora_name"],
                            "type": "lora", "path": mctx["lora_path"]})
    assert r.status_code == 400


def test_register_artifact_no_parent(admin_tok, mctx):
    row = requests.get(f"{BASE}/api/models/{mctx['nop_id']}",
                       headers=H(admin_tok), timeout=15).json()
    assert row["parent_name"] == ""


def test_artifact_size_positive(admin_tok, mctx):
    row = requests.get(f"{BASE}/api/models/{mctx['lora_id']}",
                       headers=H(admin_tok), timeout=15).json()
    assert row["size_bytes"] > 0


def test_artifact_parent_name(admin_tok, mctx):
    row = requests.get(f"{BASE}/api/models/{mctx['lora_id']}",
                       headers=H(admin_tok), timeout=15).json()
    assert row["parent_name"] == mctx["base_name"]


def test_register_artifact_merged(admin_tok):
    name = f"500mmrg-{SUF}"
    p = _mk_local(name, "merged-w")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name, "type": "merged",
                            "path": p})
    assert r.status_code == 200
    assert r.json()["type"] == "merged"


def test_register_artifact_file_path(admin_tok):
    """path 指向单个文件也应可注册"""
    p = f"/data/models/hub/500mfile-{SUF}"
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f:
        f.write("single-file-asset")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"500mfile-{SUF}",
                            "type": "lora", "path": p})
    assert r.status_code == 200
    assert r.json()["size_bytes"] > 0


def test_register_tenant_a(admin_tok, tenant_a):
    name = f"500mta-{SUF}"
    p = _mk_local(name, "ta")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30,
                      json={"project_id": tenant_a["proj_id"], "name": name,
                            "type": "lora", "path": p})
    assert r.status_code == 200


# ---------- 删除 / 隔离 ----------

def test_delete_model(admin_tok, mctx):
    aid = mctx["nop_id"]
    r = requests.delete(f"{BASE}/api/models/{aid}", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert requests.get(f"{BASE}/api/models/{aid}",
                        headers=H(admin_tok), timeout=15).status_code == 404


def test_delete_missing_404(admin_tok):
    r = requests.delete(f"{BASE}/api/models/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_delete_keeps_disk(admin_tok, mctx):
    """删除仓库登记不删磁盘文件"""
    assert os.path.exists(mctx["lora_path"]), "删除登记后磁盘文件应仍在"


def test_cross_project_get_403(admin_tok, tenant_a, mctx):
    tok = tenant_a["dev"][2]
    r = requests.get(f"{BASE}/api/models/{BASE_MODEL_ASSET_ID}",
                     headers=H(tok), timeout=15)
    assert r.status_code in (403, 404)


def test_cross_project_lineage_403(admin_tok, tenant_a, mctx):
    tok = tenant_a["dev"][2]
    r = requests.get(f"{BASE}/api/models/{BASE_MODEL_ASSET_ID}/lineage",
                     headers=H(tok), timeout=15)
    assert r.status_code in (403, 404)

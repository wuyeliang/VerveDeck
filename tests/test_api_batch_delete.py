# -*- coding: utf-8 -*-
"""批量删除端点功能测试（#97/#98 九端点统一契约）

统一契约：POST /<res>/batch-delete {"ids": [...]} →
    {"deleted": [被删 id...], "failed": [{"id": id, "error": "原因"}...]}
- 逐个执行单删逻辑，失败不阻塞其余（部分结果）
- 被引用/跨租户资源进 failed，其余照常删除

快速套件：不新增 GPU 占用。
- models 用占位基座（1 字节 README，注册即 ready）
- evals 用"坏 benchmark 版本"评测（快速 failed，不起容器）
- inference 只验证 missing 分支与部署 1 不受影响，不新建部署
- storage 对象仅 S3 可用时执行（无 S3 整组 skip，与 test_api_storage 一致）
"""
import json
import os

import pytest
import requests

from conftest import BASE, H, SUF, poll, DEFAULT_PROJECT, DEPLOY_ID, BASE_MODEL_ASSET_ID


def _bd(tok, path, ids):
    return requests.post(f"{BASE}{path}", headers=H(tok), timeout=30, json={"ids": ids})


def _mk_ds(tok, name, proj=None):
    r = requests.post(f"{BASE}/api/datasets", headers=H(tok), timeout=30,
                      json={"project_id": proj or DEFAULT_PROJECT, "name": name})
    r.raise_for_status()
    return r.json()["id"]


def _mk_placeholder_base(tok, name, proj):
    """占位基座：容器内直接建目录（/data 已挂载）→ 注册即 ready"""
    bpath = f"/data/models/hub/{name}"
    os.makedirs(bpath, exist_ok=True)
    with open(os.path.join(bpath, "README.md"), "w") as f:
        f.write(f"placeholder base {name}\n")
    r = requests.post(f"{BASE}/api/models/base", headers=H(tok), timeout=30,
                      json={"project_id": proj, "name": name, "hf_id": "local/placeholder"})
    r.raise_for_status()
    assert r.json()["status"] == "ready"
    return r.json()["id"]


@pytest.fixture(scope="module")
def bctx(admin_tok):
    """各资源上下文：占位模型 ×2 / 坏版本评测 ×2（快速 failed）/ 自定义镜像 ×2 / API Key ×2"""
    import json as _json
    c = {}
    # 占位基座 ×2（默认项目）
    c["m1"] = _mk_placeholder_base(admin_tok, f"bdm1-{SUF}", DEFAULT_PROJECT)
    c["m2"] = _mk_placeholder_base(admin_tok, f"bdm2-{SUF}", DEFAULT_PROJECT)
    # 坏版本评测 ×2（benchmark 无版本 → 快速 failed，不占 GPU）
    for k in ("e1", "e2"):
        r = requests.post(f"{BASE}/api/evals", headers=H(admin_tok), timeout=30, json={
            "project_id": DEFAULT_PROJECT, "name": f"bd{k}-{SUF}",
            "target_deployment_id": DEPLOY_ID, "benchmark": "gsm8k",
            "method": "benchmark", "limit": 1})
        assert r.status_code == 200, r.text
        c[k] = r.json()["id"]

    def _evals_done():
        rows = requests.get(f"{BASE}/api/evals", headers=H(admin_tok), timeout=15).json()
        st = {t["id"]: t["status"] for t in rows}
        return st if all(st.get(c[k]) in ("succeeded", "failed") for k in ("e1", "e2")) else None
    poll(_evals_done, timeout=180, desc="坏版本评测快速收敛")

    # 自定义镜像 ×2
    for k in ("i1", "i2"):
        r = requests.post(f"{BASE}/api/images/custom", headers=H(admin_tok), timeout=30,
                          json={"name": f"bd-{k}-{SUF}:v1", "note": "batch-delete test"})
        assert r.status_code == 200, r.text
        c[k] = r.json()["id"]
    # API Key ×2（默认项目）
    for k in ("k1", "k2"):
        r = requests.post(f"{BASE}/api/admin/projects/{DEFAULT_PROJECT}/api-keys",
                          headers=H(admin_tok), timeout=30, json={"name": f"bd-{k}-{SUF}"})
        assert r.status_code == 200, r.text
        rows = requests.get(f"{BASE}/api/admin/projects/{DEFAULT_PROJECT}/api-keys",
                            headers=H(admin_tok), timeout=15).json()
        c[k] = next(x["id"] for x in rows if x["name"] == f"bd-{k}-{SUF}")
    return c


# ---------- 数据集 ----------

def test_bd_datasets_ok(admin_tok):
    d1 = _mk_ds(admin_tok, f"bdd1-{SUF}")
    d2 = _mk_ds(admin_tok, f"bdd2-{SUF}")
    r = _bd(admin_tok, "/api/datasets/batch-delete", [d1, d2])
    assert r.status_code == 200, r.text
    body = r.json()
    assert sorted(body["deleted"]) == sorted([d1, d2]) and body["failed"] == []
    assert requests.get(f"{BASE}/api/datasets/{d1}", headers=H(admin_tok),
                        timeout=15).status_code == 404
    assert requests.get(f"{BASE}/api/datasets/{d2}", headers=H(admin_tok),
                        timeout=15).status_code == 404


def test_bd_datasets_mixed_valid_missing(admin_tok):
    d1 = _mk_ds(admin_tok, f"bdd3-{SUF}")
    r = _bd(admin_tok, "/api/datasets/batch-delete", [d1, 999999])
    body = r.json()
    assert body["deleted"] == [d1]
    assert len(body["failed"]) == 1 and body["failed"][0]["id"] == 999999
    assert body["failed"][0]["error"]


def test_bd_datasets_referenced_by_job(admin_tok):
    """被训练任务引用的数据集进 failed；取消并删除任务后可删"""
    import json as _json
    dsid = _mk_ds(admin_tok, f"bdd4-{SUF}")
    rows = [{"instruction": "q", "input": "", "output": "a"}]
    files = {"file": ("r.jsonl", _json.dumps(rows, ensure_ascii=False).encode(),
                      "application/octet-stream")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      headers=H(admin_tok), timeout=60, params={"fmt": "alpaca"}, files=files)
    r.raise_for_status()
    vid = r.json()["id"]
    # 排队训练任务（GPU 被常驻部署占用 → 保持 queued）
    r = requests.post(f"{BASE}/api/training/jobs", headers=H(admin_tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT, "name": f"bdjob-{SUF}", "stage": "sft",
        "base_model_id": BASE_MODEL_ASSET_ID, "dataset_version_ids": [vid],
        "gpu_count": 1, "priority": 5})
    assert r.status_code == 200, r.text
    jid = r.json()["id"]
    try:
        body = _bd(admin_tok, "/api/datasets/batch-delete", [dsid]).json()
        assert body["deleted"] == [] and body["failed"][0]["id"] == dsid
        assert "训练任务" in body["failed"][0]["error"]
    finally:
        requests.post(f"{BASE}/api/training/jobs/{jid}/cancel",
                      headers=H(admin_tok), timeout=30)
        requests.delete(f"{BASE}/api/training/jobs/{jid}",
                        headers=H(admin_tok), timeout=30)
    body = _bd(admin_tok, "/api/datasets/batch-delete", [dsid]).json()
    assert body["deleted"] == [dsid] and body["failed"] == []


def test_bd_datasets_viewer_403(admin_tok, tenant_a):
    r = _bd(tenant_a["viewer"][2], "/api/datasets/batch-delete", [1])
    assert r.status_code == 403


# ---------- 模型 ----------

def test_bd_models_ok(admin_tok, bctx):
    r = _bd(admin_tok, "/api/models/batch-delete", [bctx["m1"], bctx["m2"]])
    body = r.json()
    assert sorted(body["deleted"]) == sorted([bctx["m1"], bctx["m2"]]) and body["failed"] == []
    # 磁盘清理：占位目录随资产删除
    for n in (f"bdm1-{SUF}", f"bdm2-{SUF}"):
        assert not os.path.isdir(f"/data/models/hub/{n}"), f"占位目录未清理: {n}"


def test_bd_models_in_use_by_deploy(admin_tok):
    """被运行中部署引用的模型进 failed，部署 1 本身不受影响"""
    dep = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                       headers=H(admin_tok), timeout=15).json()
    aid = dep["model_asset_id"]
    body = _bd(admin_tok, "/api/models/batch-delete", [aid]).json()
    assert body["deleted"] == [] and body["failed"][0]["id"] == aid
    assert "部署" in body["failed"][0]["error"]
    d2 = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                      headers=H(admin_tok), timeout=15).json()
    assert d2["status"] == "running", "批量删除不应波及运行中部署"


def test_bd_models_mixed(admin_tok):
    m = _mk_placeholder_base(admin_tok, f"bdm3-{SUF}", DEFAULT_PROJECT)
    body = _bd(admin_tok, "/api/models/batch-delete", [m, 999999]).json()
    assert body["deleted"] == [m] and len(body["failed"]) == 1


# ---------- 评测 ----------

def test_bd_evals_ok(admin_tok, bctx):
    body = _bd(admin_tok, "/api/evals/batch-delete",
               [bctx["e1"], bctx["e2"], 999999]).json()
    assert sorted(body["deleted"]) == sorted([bctx["e1"], bctx["e2"]])
    assert len(body["failed"]) == 1 and body["failed"][0]["id"] == 999999


# ---------- 自定义镜像 ----------

def test_bd_images_custom_ok(admin_tok, bctx):
    body = _bd(admin_tok, "/api/images/custom/batch-delete",
               [bctx["i1"], bctx["i2"]]).json()
    assert sorted(body["deleted"]) == sorted([bctx["i1"], bctx["i2"]]) and body["failed"] == []
    rows = requests.get(f"{BASE}/api/images/custom", headers=H(admin_tok), timeout=15).json()
    ids = [x["id"] for x in rows]
    assert bctx["i1"] not in ids and bctx["i2"] not in ids


def test_bd_images_custom_mixed(admin_tok):
    r = requests.post(f"{BASE}/api/images/custom", headers=H(admin_tok), timeout=30,
                      json={"name": f"bd-i3-{SUF}:v1"})
    cid = r.json()["id"]
    body = _bd(admin_tok, "/api/images/custom/batch-delete", [cid, 999999]).json()
    assert body["deleted"] == [cid] and len(body["failed"]) == 1


def test_bd_images_custom_viewer_403(admin_tok, tenant_a):
    """镜像管理仅超管：developer 403"""
    r = _bd(tenant_a["dev"][2], "/api/images/custom/batch-delete", [1])
    assert r.status_code == 403


# ---------- API Key（吊销语义） ----------

def test_bd_apikeys_revoke(admin_tok, bctx):
    body = _bd(admin_tok, f"/api/admin/projects/{DEFAULT_PROJECT}/api-keys/batch-delete",
               [bctx["k1"], bctx["k2"]]).json()
    assert sorted(body["deleted"]) == sorted([bctx["k1"], bctx["k2"]]) and body["failed"] == []
    rows = requests.get(f"{BASE}/api/admin/projects/{DEFAULT_PROJECT}/api-keys",
                        headers=H(admin_tok), timeout=15).json()
    st = {x["id"]: x["is_active"] for x in rows}
    assert st[bctx["k1"]] is False and st[bctx["k2"]] is False


def test_bd_apikeys_missing(admin_tok):
    body = _bd(admin_tok, f"/api/admin/projects/{DEFAULT_PROJECT}/api-keys/batch-delete",
               [999999]).json()
    assert body["deleted"] == [] and body["failed"] == []


# ---------- 护栏规则 ----------

def test_bd_guard_rules(admin_tok):
    rids = []
    for i in (1, 2):
        r = requests.post(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15,
                          json={"pattern": f"bds{SUF}{i}", "scope": "both"})
        r.raise_for_status()
        rids.append(r.json()["id"])
    body = _bd(admin_tok, "/api/admin/guard-rules/batch-delete",
               rids + [999999]).json()
    assert sorted(body["deleted"]) == sorted(rids) and body["failed"] == []
    rows = requests.get(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15).json()
    assert all(x["id"] not in rids for x in rows)


# ---------- 告警规则 ----------

def test_bd_alert_rules(admin_tok):
    rids = []
    for i in (1, 2):
        r = requests.post(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=30,
                          json={"name": f"bda{SUF}{i}", "kind": "threshold",
                                "metric": "gpu_util", "op": ">", "threshold": 99,
                                "severity": "warning", "enabled": False})
        r.raise_for_status()
        rids.append(r.json()["id"])
    body = _bd(admin_tok, "/api/admin/alerts/rules/batch-delete", rids).json()
    assert sorted(body["deleted"]) == sorted(rids) and body["failed"] == []
    rows = requests.get(f"{BASE}/api/admin/alerts/rules", headers=H(admin_tok), timeout=15).json()
    assert all(x["id"] not in rids for x in rows)


# ---------- 推理部署（missing 分支，不触碰部署 1） ----------

def test_bd_deploys_missing_only(admin_tok):
    body = _bd(admin_tok, "/api/inference/deployments/batch-delete", [999999, 888888]).json()
    assert body["deleted"] == [] and len(body["failed"]) == 2
    dep = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                       headers=H(admin_tok), timeout=15).json()
    assert dep["status"] == "running"


# ---------- 存储对象（S3 可用时） ----------

def _s3_enabled() -> bool:
    return bool(os.environ.get("S3_ENDPOINT", ""))


@pytest.mark.skipif(not _s3_enabled(), reason="S3_ENDPOINT 未配置（纯本地模式）")
def test_bd_storage_objects(admin_tok):
    from minio import Minio
    url = os.environ.get("S3_ENDPOINT", "")
    if "://" in url:
        secure = url.startswith("https")
        host = url.split("//", 1)[1].split("/", 1)[0]
    else:
        secure, host = False, (url or "rustfs:9000")
    cli = Minio(host, access_key=os.environ.get("S3_ACCESS_KEY", "rustfsadmin"),
                secret_key=os.environ.get("S3_SECRET_KEY", "rustfsadmin"), secure=secure)
    # 找主对象存储的桶（与 test_api_storage 一致：datasets 桶承载数据集对象）
    bucket = os.environ.get("S3_BUCKET", "datasets")
    prefix = f"bdtest-{SUF}/"
    import io
    for n in ("a.txt", "b.txt"):
        cli.put_object(bucket, prefix + n, io.BytesIO(b"x"), length=1,
                       content_type="text/plain")
    body = requests.post(f"{BASE}/api/storage/objects/batch-delete",
                         headers=H(admin_tok), timeout=60,
                         json={"names": [prefix], "backend": 0}).json()
    assert body["deleted"] == [prefix] and body["failed"] == []
    left = list(cli.list_objects(bucket, prefix=prefix, recursive=True))
    assert left == [], f"前缀未清空: {left}"


# ---------- 空请求体 ----------

def test_bd_empty_ids_422(admin_tok):
    r = _bd(admin_tok, "/api/datasets/batch-delete", [])
    # 空 ids：各端点均直接返回空结果（422 亦属可接受，取决于校验策略）
    assert r.status_code in (200, 422)

# -*- coding: utf-8 -*-
"""数据工程 API：CRUD / 版本上传 / 预览 / DVC / 清洗脱敏 / 合成 / MinerU 解析 / Argilla

受控真实任务：3 次清洗（纯 CPU 快速）+ 1 次合成（1 次 LLM 调用）+ 2 次 MinerU 解析（.md）
"""
import json

import pytest
import requests

from conftest import (BASE, H, SUF, poll, retry_call, DEFAULT_PROJECT,
                      ALPACA_ROWS, DPO_ROWS, DEPLOY_NAME)


def _mk_ds(tok, name, proj=None):
    r = requests.post(f"{BASE}/api/datasets", headers=H(tok), timeout=30,
                      json={"project_id": proj or DEFAULT_PROJECT, "name": name})
    r.raise_for_status()
    return r.json()["id"]


def _upload(tok, dsid, rows, fmt="alpaca", fname="r.jsonl", note=""):
    payload = "\n".join(json.dumps(x, ensure_ascii=False) for x in rows)
    files = {"file": (fname, payload.encode("utf-8"), "application/octet-stream")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      headers=H(tok), timeout=60,
                      params={"fmt": fmt, "note": note}, files=files)
    r.raise_for_status()
    return r.json()


def _task(admin_tok, tid):
    rows = requests.get(f"{BASE}/api/datasets/tasks", headers=H(admin_tok), timeout=15).json()
    return next(t for t in rows if t["id"] == tid)


def _wait_task(admin_tok, tid, timeout=300):
    def check():
        t = _task(admin_tok, tid)
        return t if t["status"] in ("done", "failed") else None
    return poll(check, timeout=timeout, desc=f"数据任务 {tid} 完成")


@pytest.fixture(scope="module")
def dctx(admin_tok, dsctx):
    return dict(dsctx)  # alpaca_ds / alpaca_ver / dpo_ds / dpo_ver


# ---------- CRUD ----------

def test_list_contains_ctx(admin_tok, dctx):
    rows = requests.get(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=15).json()
    ids = [d["id"] for d in rows]
    assert dctx["alpaca_ds"] in ids and dctx["dpo_ds"] in ids


def test_list_project_filter(admin_tok, dctx):
    rows = requests.get(f"{BASE}/api/datasets",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    assert all(d["project_id"] == DEFAULT_PROJECT for d in rows)
    assert any(d["id"] == dctx["alpaca_ds"] for d in rows)


def test_list_includes_versions_count(admin_tok, dctx):
    rows = requests.get(f"{BASE}/api/datasets",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    row = next(d for d in rows if d["id"] == dctx["alpaca_ds"])
    assert row["versions"] >= 1


def test_create(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-crud-{SUF}")
    assert dsid
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_create_duplicate_400(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=15,
                      json={"project_id": DEFAULT_PROJECT,
                            "name": f"500alpaca-{SUF}"})
    assert r.status_code == 400


def test_create_missing_422(admin_tok):
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=15,
                      json={"project_id": DEFAULT_PROJECT})
    assert r.status_code == 422


def test_create_tenant_a_dev(admin_tok, tenant_a):
    tok = tenant_a["dev"][2]
    dsid = _mk_ds(tok, f"tdev-ds-{SUF}", tenant_a["proj_id"])
    assert dsid


def test_get(admin_tok, dctx):
    r = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == f"500alpaca-{SUF}"
    assert isinstance(body["versions"], list) and body["versions"]


def test_get_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/datasets/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_delete(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-del-{SUF}")
    r = requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert requests.get(f"{BASE}/api/datasets/{dsid}",
                        headers=H(admin_tok), timeout=15).status_code == 404


def test_delete_missing_404(admin_tok):
    r = requests.delete(f"{BASE}/api/datasets/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_dataset_description_stored(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-descd-{SUF}")
    body = requests.get(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15).json()
    assert body["description"] == ""
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


# ---------- 存储位置选择 ----------

@pytest.fixture()
def ds_loc_backend(admin_tok):
    """存储位置测试用 juicefs 后端（名称与 test_api_storage 的 jfs-* 区分，避免唯一名冲突）"""
    r = requests.post(f"{BASE}/api/storage/backends", headers=H(admin_tok), timeout=60,
                      json={"name": f"dsloc-{SUF}", "type": "juicefs",
                            "config": {"path": "/data/juicefs"}})
    r.raise_for_status()
    bid = r.json()["id"]
    yield bid
    requests.delete(f"{BASE}/api/storage/backends/{bid}", headers=H(admin_tok), timeout=30)


def test_create_with_storage_location(admin_tok, ds_loc_backend):
    dsid = None
    try:
        r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                          json={"project_id": DEFAULT_PROJECT, "name": f"dslocd-{SUF}",
                                "storage_backend_id": ds_loc_backend})
        r.raise_for_status()
        dsid = r.json()["id"]
        rows = requests.get(f"{BASE}/api/datasets",
                            params={"project_id": DEFAULT_PROJECT},
                            headers=H(admin_tok), timeout=15).json()
        d = next(x for x in rows if x["id"] == dsid)
        assert d["storage_backend_id"] == ds_loc_backend
        assert d["storage_backend"]  # 非空显示名（名称（类型））
    finally:
        if dsid:
            requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)


def test_upload_version_repo_under_mount(admin_tok, ds_loc_backend):
    """path 型存储位置：版本仓库直接建在挂载点下；删除数据集后仓库目录一并清理"""
    import os
    import re
    name = f"dslocrepo-{SUF}"
    safe = re.sub(r"[^\w\-]", "_", name)
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name,
                            "storage_backend_id": ds_loc_backend})
    r.raise_for_status()
    dsid = r.json()["id"]
    try:
        _upload(admin_tok, dsid, ALPACA_ROWS[:2])
        p = f"/data/juicefs/datasets/p{DEFAULT_PROJECT}/{safe}/v1/data.jsonl"
        assert os.path.exists(p), f"版本文件应落在挂载点下: {p}"
    finally:
        requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)
    repo = f"/data/juicefs/datasets/p{DEFAULT_PROJECT}/{safe}"
    assert not os.path.exists(repo), "删除数据集后挂载点下仓库目录应清理"


def test_default_dataset_stays_in_datadir(admin_tok):
    """未选存储位置 → 仓库仍在平台默认 data_dir 下（历史行为不变）"""
    import os
    data_dir = os.environ.get("DATA_DIR", "/data")
    name = f"dslocreg-{SUF}"
    dsid = _mk_ds(admin_tok, name)
    try:
        _upload(admin_tok, dsid, ALPACA_ROWS[:1])
        assert os.path.exists(f"{data_dir}/datasets/p{DEFAULT_PROJECT}/{name}/v1/data.jsonl")
    finally:
        requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)


def _main_bucket_keys(prefix):
    """主对象存储桶指定前缀下的 key 列表（主存储未启用/不可达返回 None，由测试侧降级 skip）"""
    try:
        from app.config import settings
        from app.services import storage
    except Exception:
        return None
    if not storage.enabled():
        return None
    try:
        storage.ensure_bucket()
        return [o.object_name for o in storage.client().list_objects(
            settings.s3_bucket, prefix=prefix, recursive=True)]
    except Exception:
        return None


def test_path_type_dataset_no_main_mirror(admin_tok, ds_loc_backend):
    """path 型存储位置：版本文件只落在挂载点，主对象存储桶不写镜像副副本（避免双份占用）"""
    name = f"dsmirr-{SUF}"
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name,
                            "storage_backend_id": ds_loc_backend})
    r.raise_for_status()
    dsid = r.json()["id"]
    try:
        _upload(admin_tok, dsid, ALPACA_ROWS[:2])
        keys = _main_bucket_keys(f"datasets/p{DEFAULT_PROJECT}/{name}/")
        if keys is None:
            pytest.skip("主对象存储不可用，跳过镜像断言")
        assert keys == [], f"主桶不应含 path 型数据集镜像副本: {keys[:5]}"
    finally:
        requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)


def test_default_dataset_local_only(admin_tok):
    """未选存储位置：版本文件仅存本地 /data（单副本语义），主对象存储桶不写镜像"""
    import os
    name = f"dslonly-{SUF}"
    dsid = _mk_ds(admin_tok, name)
    try:
        _upload(admin_tok, dsid, ALPACA_ROWS[:1])
        data_dir = os.environ.get("DATA_DIR", "/data")
        assert os.path.exists(
            f"{data_dir}/datasets/p{DEFAULT_PROJECT}/{name}/v1/data.jsonl"), \
            "默认数据集版本文件应存本地 /data"
        keys = _main_bucket_keys(f"datasets/p{DEFAULT_PROJECT}/{name}/")
        if keys is None:
            pytest.skip("主对象存储不可用，跳过不镜像断言")
        assert keys == [], f"默认数据集不应镜像到主桶（单副本语义）: {keys[:5]}"
    finally:
        requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)


@pytest.fixture()
def ds_obj_backend(admin_tok):
    """存储位置测试用对象存储后端（平台 rustfs + 独立临时桶，避免污染主桶；测后清理）"""
    import os
    url = os.environ.get("S3_ENDPOINT", "")
    if not url:
        pytest.skip("S3 未启用（纯本地模式）")
    host = url.split("//", 1)[1].split("/", 1)[0] if "://" in url else url
    bucket = f"dsloc-{SUF}-obj"
    r = requests.post(f"{BASE}/api/storage/backends", headers=H(admin_tok), timeout=60,
                      json={"name": f"dsobj-{SUF}", "type": "object",
                            "config": {"endpoint": host,
                                       "access_key": os.environ.get("S3_ACCESS_KEY", "rustfsadmin"),
                                       "secret_key": os.environ.get("S3_SECRET_KEY", "rustfsadmin"),
                                       "bucket": bucket}})
    r.raise_for_status()
    bid = r.json()["id"]
    yield {"id": bid, "bucket": bucket}
    requests.delete(f"{BASE}/api/storage/backends/{bid}", headers=H(admin_tok), timeout=30)


def _obj_bucket_keys(bucket, prefix):
    from test_api_storage import _minio_client
    c = _minio_client()
    return [o.object_name for o in c.list_objects(bucket, prefix=prefix, recursive=True)]


def test_object_type_dataset_bucket_only(admin_tok, ds_obj_backend):
    """object 型存储位置：唯一副本在桶内，全量上传后本地工作副本清理；预览用时自动拉回；
    删除数据集后桶前缀一并清理（不依赖本地目录存在）"""
    import os
    name = f"dsobjd-{SUF}"
    data_dir = os.environ.get("DATA_DIR", "/data")
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name,
                            "storage_backend_id": ds_obj_backend["id"]})
    r.raise_for_status()
    dsid = r.json()["id"]
    try:
        _upload(admin_tok, dsid, ALPACA_ROWS[:2])
        # 本地工作副本已清理（桶内为唯一权威副本）
        vfile = f"{data_dir}/datasets/p{DEFAULT_PROJECT}/{name}/v1/data.jsonl"
        assert not os.path.exists(vfile), f"本地工作副本应已清理（单副本）: {vfile}"
        # 桶内含该版本文件
        keys = _obj_bucket_keys(ds_obj_backend["bucket"],
                                f"datasets/p{DEFAULT_PROJECT}/{name}/")
        assert any(k.endswith("/v1/data.jsonl") for k in keys), \
            f"桶内应含版本文件: {keys[:5]}"
        # 预览仍可用：本地缓存缺失 → 自动从桶拉回（单副本读取链路）
        vers = requests.get(f"{BASE}/api/datasets/{dsid}/versions",
                            headers=H(admin_tok), timeout=15).json()
        v = next(x for x in vers if x["version"] == 1)
        r2 = requests.get(f"{BASE}/api/datasets/{dsid}/versions/{v['id']}/samples",
                          headers=H(admin_tok), timeout=60)
        assert r2.status_code == 200, f"预览应自动从桶拉回: {r2.text[:200]}"
        assert r2.json()["total"] == len(ALPACA_ROWS[:2])
    finally:
        requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)
    # 删除后桶前缀清理（此时本地仓库目录已不存在，验证 force 删除路径）
    keys = _obj_bucket_keys(ds_obj_backend["bucket"],
                            f"datasets/p{DEFAULT_PROJECT}/{name}/")
    assert keys == [], f"删除数据集后桶前缀应清理: {keys[:5]}"


def test_builtin_object_dataset_bucket_only(admin_tok):
    """内置对象存储（storage_backend_id=0，平台 rustfs 主桶）：选它后单副本存主桶、
    本地工作副本清理、预览自动拉回、删除后主桶前缀清理"""
    import os
    if not os.environ.get("S3_ENDPOINT", ""):
        pytest.skip("S3 未启用（纯本地模式）")
    from test_api_storage import _minio_client
    main_bucket = os.environ.get("S3_BUCKET", "vervedeck-datasets")
    name = f"dsbtd-{SUF}"
    data_dir = os.environ.get("DATA_DIR", "/data")
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name,
                            "storage_backend_id": 0})
    r.raise_for_status()
    dsid = r.json()["id"]
    prefix = f"datasets/p{DEFAULT_PROJECT}/{name}/"
    try:
        _upload(admin_tok, dsid, ALPACA_ROWS[:2])
        vfile = f"{data_dir}/datasets/p{DEFAULT_PROJECT}/{name}/v1/data.jsonl"
        assert not os.path.exists(vfile), f"本地工作副本应已清理（单副本）: {vfile}"
        c = _minio_client()
        keys = [o.object_name for o in c.list_objects(main_bucket, prefix=prefix, recursive=True)]
        assert any(k.endswith("/v1/data.jsonl") for k in keys), f"主桶应含版本文件: {keys[:5]}"
        vers = requests.get(f"{BASE}/api/datasets/{dsid}/versions",
                            headers=H(admin_tok), timeout=15).json()
        v = next(x for x in vers if x["version"] == 1)
        r2 = requests.get(f"{BASE}/api/datasets/{dsid}/versions/{v['id']}/samples",
                          headers=H(admin_tok), timeout=60)
        assert r2.status_code == 200, f"预览应自动从主桶拉回: {r2.text[:200]}"
        assert r2.json()["total"] == len(ALPACA_ROWS[:2])
    finally:
        requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)
    c = _minio_client()
    keys = [o.object_name for o in c.list_objects(main_bucket, prefix=prefix, recursive=True)]
    assert keys == [], f"删除数据集后主桶前缀应清理: {keys[:5]}"


# ---------- 上传 / 版本 ----------

def test_upload_alpaca_count(admin_tok, dctx):
    vers = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions",
                        headers=H(admin_tok), timeout=15).json()
    v = next(x for x in vers if x["id"] == dctx["alpaca_ver"])
    assert v["sample_count"] == len(ALPACA_ROWS)
    assert v["fmt"] == "alpaca"


def test_upload_dpo_filters_empty_prompt(admin_tok, dctx):
    vers = requests.get(f"{BASE}/api/datasets/{dctx['dpo_ds']}/versions",
                        headers=H(admin_tok), timeout=15).json()
    v = next(x for x in vers if x["id"] == dctx["dpo_ver"])
    # 3 行中 1 行 prompt 为空被过滤
    assert v["sample_count"] == len(DPO_ROWS) - 1


def test_upload_json_array(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-ja-{SUF}")
    files = {"file": ("a.json", json.dumps(ALPACA_ROWS[:2], ensure_ascii=False).encode(),
                      "application/json")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "alpaca"}, files=files)
    assert r.status_code == 200 and r.json()["sample_count"] == 2
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_upload_bad_json_400(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-bj-{SUF}")
    files = {"file": ("b.json", b"[{not json", "application/json")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "alpaca"}, files=files)
    assert r.status_code == 400
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_upload_all_invalid_400(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-iv-{SUF}")
    files = {"file": ("c.jsonl",
                      json.dumps({"foo": 1}).encode() + b"\n" +
                      json.dumps({"bar": 2}).encode(), "application/octet-stream")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "alpaca"}, files=files)
    assert r.status_code == 400
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_upload_missing_file_422(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "alpaca"}, files={})
    assert r.status_code in (400, 422)


def test_upload_missing_ds_404(admin_tok):
    files = {"file": ("x.jsonl", b'{"instruction":"a","output":"b"}',
                      "application/octet-stream")}
    r = requests.post(f"{BASE}/api/datasets/999999/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "alpaca"}, files=files)
    assert r.status_code == 404


def test_upload_empty_400(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-emp-{SUF}")
    files = {"file": ("e.jsonl", b"", "application/octet-stream")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "alpaca"}, files=files)
    assert r.status_code == 400
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_upload_csv_like_jsonl(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-csv-{SUF}")
    files = {"file": ("d.csv",
                      b'{"instruction":"q1","input":"","output":"a1"}\n'
                      b'{"instruction":"q2","input":"","output":"a2"}',
                      "application/octet-stream")}
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/upload",
                      headers=H(admin_tok), timeout=60,
                      params={"fmt": "alpaca"}, files=files)
    assert r.status_code == 200 and r.json()["sample_count"] == 2
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_upload_note_stored(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-note-{SUF}")
    v = _upload(admin_tok, dsid, ALPACA_ROWS[:1], note="notetest")
    vers = requests.get(f"{BASE}/api/datasets/{dsid}/versions",
                        headers=H(admin_tok), timeout=15).json()
    assert next(x for x in vers if x["id"] == v["id"])["note"] == "notetest"
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_upload_second_version_number(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-v2-{SUF}")
    v1 = _upload(admin_tok, dsid, ALPACA_ROWS[:1])
    v2 = _upload(admin_tok, dsid, ALPACA_ROWS[:2])
    assert v1["version"] == 1 and v2["version"] == 2
    # 列表降序
    vers = requests.get(f"{BASE}/api/datasets/{dsid}/versions",
                        headers=H(admin_tok), timeout=15).json()
    assert [v["version"] for v in vers] == [2, 1]
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_get_versions_includes_fmt(admin_tok, dctx):
    vers = requests.get(f"{BASE}/api/datasets/{dctx['dpo_ds']}/versions",
                        headers=H(admin_tok), timeout=15).json()
    assert vers and all(v["fmt"] == "dpo" for v in vers)


# ---------- 预览 / 原始文档 ----------

def test_preview_total_and_page(admin_tok, dctx):
    r = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/{dctx['alpaca_ver']}/samples",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == len(ALPACA_ROWS) and body["page"] == 1


def test_preview_pagination(admin_tok, dctx):
    r = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/{dctx['alpaca_ver']}/samples",
                     params={"page": 2, "size": 2}, headers=H(admin_tok), timeout=15)
    body = r.json()
    assert body["page"] == 2 and len(body["items"]) == 2
    assert body["items"][0] != ALPACA_ROWS[0] or len(ALPACA_ROWS) <= 2


def test_preview_missing_version_404(admin_tok, dctx):
    r = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/999999/samples",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_preview_alpaca_fields(admin_tok, dctx):
    body = requests.get(
        f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/{dctx['alpaca_ver']}/samples",
        headers=H(admin_tok), timeout=15).json()
    row = body["items"][0]
    assert "instruction" in row and "output" in row


def test_preview_dpo_fields(admin_tok, dctx):
    body = requests.get(
        f"{BASE}/api/datasets/{dctx['dpo_ds']}/versions/{dctx['dpo_ver']}/samples",
        headers=H(admin_tok), timeout=15).json()
    row = body["items"][0]
    assert "chosen" in row and "rejected" in row


def test_raw_missing_for_upload_404(admin_tok, dctx):
    r = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/{dctx['alpaca_ver']}/raw",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


# ---------- DVC ----------

def test_commit(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-dvc-{SUF}")
    v = _upload(admin_tok, dsid, ALPACA_ROWS[:2], note="dvc test")
    r = requests.post(f"{BASE}/api/datasets/{dsid}/versions/{v['id']}/commit",
                      headers=H(admin_tok), timeout=120)
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True and r.json().get("dvc_rev")
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_commit_status_committed(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-dvc2-{SUF}")
    v = _upload(admin_tok, dsid, ALPACA_ROWS[:1])
    requests.post(f"{BASE}/api/datasets/{dsid}/versions/{v['id']}/commit",
                  headers=H(admin_tok), timeout=120)
    vers = requests.get(f"{BASE}/api/datasets/{dsid}/versions",
                        headers=H(admin_tok), timeout=15).json()
    assert next(x for x in vers if x["id"] == v["id"])["status"] == "committed"
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_commit_bad_version_404(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/999999/commit",
                      headers=H(admin_tok), timeout=30)
    assert r.status_code == 404


def test_dvc_log_after_commit(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-dvcl-{SUF}")
    v = _upload(admin_tok, dsid, ALPACA_ROWS[:1])
    requests.post(f"{BASE}/api/datasets/{dsid}/versions/{v['id']}/commit",
                  headers=H(admin_tok), timeout=120)
    rows = requests.get(f"{BASE}/api/datasets/{dsid}/dvc-log",
                        headers=H(admin_tok), timeout=30).json()
    assert rows, "DVC 日志应为空以外的内容"
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


def test_dvc_log_fresh_empty(admin_tok):
    dsid = _mk_ds(admin_tok, f"adm-dvcfe-{SUF}")
    rows = requests.get(f"{BASE}/api/datasets/{dsid}/dvc-log",
                        headers=H(admin_tok), timeout=30).json()
    assert rows == []
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15)


# ---------- 任务查询 ----------

def test_tasks_list_200(admin_tok):
    rows = requests.get(f"{BASE}/api/datasets/tasks", headers=H(admin_tok), timeout=15).json()
    assert isinstance(rows, list)


def test_tasks_dataset_filter(admin_tok, dctx):
    rows = requests.get(f"{BASE}/api/datasets/tasks",
                        params={"dataset_id": dctx["alpaca_ds"]},
                        headers=H(admin_tok), timeout=15).json()
    assert all(t["dataset_id"] == dctx["alpaca_ds"] for t in rows)


def test_tasks_order_desc(admin_tok):
    rows = requests.get(f"{BASE}/api/datasets/tasks", headers=H(admin_tok), timeout=15).json()
    ids = [t["id"] for t in rows]
    assert ids == sorted(ids, reverse=True)


# ---------- 清洗（真实任务，纯 CPU） ----------

def test_clean_real_dedup_pii(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/clean", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": dctx["alpaca_ver"]})
    assert r.status_code == 200
    t = _wait_task(admin_tok, r.json()["task_id"])
    assert t["status"] == "done", f"clean 失败: {t['error']}"
    stats = t["result"]
    assert stats["total"] == len(ALPACA_ROWS)
    assert stats["kept"] == 5, f"应去重 1 条: {stats}"
    assert stats["masked"] == 2, f"应脱敏 2 条(手机/邮箱 + 身份证/银行卡): {stats}"
    # 输出版本内容确认手机号已脱敏
    vers = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions",
                        headers=H(admin_tok), timeout=15).json()
    out = next(v for v in vers if v["id"] == t["output_version_id"])
    assert out["sample_count"] == 5
    body = requests.get(
        f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/{out['id']}/samples",
        params={"size": 50}, headers=H(admin_tok), timeout=15).json()
    text = json.dumps(body["items"], ensure_ascii=False)
    assert "13812345678" not in text, "手机号未被脱敏"


def test_clean_min_len(admin_tok, dctx):
    # min_len=5：仅第 6 行 "A"（len 1）被滤；第 2 行 9 字符需保留
    r = requests.post(f"{BASE}/api/datasets/clean", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": dctx["alpaca_ver"],
                            "pii": [], "dedup": False, "min_len": 5})
    t = _wait_task(admin_tok, r.json()["task_id"])
    assert t["status"] == "done", t["error"]
    assert t["result"]["kept"] == len(ALPACA_ROWS) - 1, f"应过滤短样本: {t['result']}"


def test_clean_dedup_false(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/clean", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": dctx["alpaca_ver"],
                            "dedup": False})
    t = _wait_task(admin_tok, r.json()["task_id"])
    assert t["status"] == "done", t["error"]
    assert t["result"]["kept"] == len(ALPACA_ROWS), f"不去重应保留全部: {t['result']}"


def test_clean_pii_subset_email_only(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/clean", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": dctx["alpaca_ver"],
                            "pii": ["email"], "dedup": False})
    t = _wait_task(admin_tok, r.json()["task_id"])
    assert t["status"] == "done", t["error"]
    vers = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions",
                        headers=H(admin_tok), timeout=15).json()
    out = next(v for v in vers if v["id"] == t["output_version_id"])
    body = requests.get(
        f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/{out['id']}/samples",
        params={"size": 50}, headers=H(admin_tok), timeout=15).json()
    text = json.dumps(body["items"], ensure_ascii=False)
    assert "test01@example.com" not in text, "邮箱未脱敏"
    assert "13812345678" in text, "仅脱敏邮箱时手机号应保留"


def test_clean_custom_regex(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/clean", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": dctx["alpaca_ver"],
                            "pii": [], "dedup": False,
                            "custom_regex": [{"pattern": "13812345678", "replace": "[TEL]"}]})
    t = _wait_task(admin_tok, r.json()["task_id"])
    assert t["status"] == "done", t["error"]
    vers = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions",
                        headers=H(admin_tok), timeout=15).json()
    out = next(v for v in vers if v["id"] == t["output_version_id"])
    body = requests.get(
        f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/{out['id']}/samples",
        params={"size": 50}, headers=H(admin_tok), timeout=15).json()
    text = json.dumps(body["items"], ensure_ascii=False)
    assert "[TEL]" in text and "13812345678" not in text


def test_clean_stats_fields(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/clean", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": dctx["alpaca_ver"]})
    t = _wait_task(admin_tok, r.json()["task_id"])
    assert set(("total", "kept", "dropped", "masked")) <= set(t["result"].keys())


def test_clean_bad_source_404(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/clean", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"], "source_version_id": 999999})
    assert r.status_code == 404


def test_clean_cross_ds_version_404(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/clean", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": dctx["dpo_ver"]})
    assert r.status_code == 404


# ---------- 合成（真实任务，1 次 LLM 调用） ----------

def test_synth_real(admin_tok, dctx):
    # flaky 对策（#80）：synth 依赖常驻部署的真实 LLM 调用，全量运行窗口内上游
    # 偶发超时/重启会致任务 failed（单跑均过、全量偶发失败）。整段重试 3 次：
    # 每次提交全新合成任务（输出为新版本，互不污染）。
    # 部署按名解析（原硬编码 deployment_id=1，部署重建后 ID 漂移会误判 400）。
    deps = requests.get(f"{BASE}/api/inference/deployments", headers=H(admin_tok),
                        timeout=15).json()
    dep = next((d for d in deps if d.get("name") == DEPLOY_NAME
                and d.get("status") == "running"), None)
    assert dep, f"常驻部署 {DEPLOY_NAME} 未运行（synth 需真实 LLM 部署）"

    def synth_once():
        r = requests.post(f"{BASE}/api/datasets/synth", headers=H(admin_tok), timeout=30,
                          json={"dataset_id": dctx["alpaca_ds"],
                                "source_version_id": dctx["alpaca_ver"],
                                "mode": "qa", "n_per_chunk": 1, "deployment_id": dep["id"]})
        assert r.status_code == 200, r.text
        t = _wait_task(admin_tok, r.json()["task_id"], timeout=600)
        assert t["status"] == "done", f"synth 失败: {t['error']}"
        assert t["result"]["generated"] >= 1
        vers = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions",
                            headers=H(admin_tok), timeout=15).json()
        out = next(v for v in vers if v["id"] == t["output_version_id"])
        assert out["sample_count"] >= 1
    retry_call(synth_once, tries=3, delay=20, desc="真实合成（1 次 LLM 调用）")


def test_synth_missing_target_400(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/synth", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": dctx["alpaca_ver"]})
    assert r.status_code == 400


def test_synth_bad_deploy_400(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/synth", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": dctx["alpaca_ver"],
                            "deployment_id": 999999})
    assert r.status_code == 400


def test_synth_bad_source_404(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/synth", headers=H(admin_tok), timeout=30,
                      json={"dataset_id": dctx["alpaca_ds"],
                            "source_version_id": 999999, "deployment_id": 1})
    assert r.status_code == 404


# ---------- MinerU 解析（真实任务，.md 输入） ----------

DOC_MD = """# 测试文档标题

第一段：VerveDeck 是一个大模型微调训练平台，支持 CPT、SFT、DPO 与模型合并，
并内置数据集版本控制、自动化评测与推理部署能力。

第二段：vLLM 是高性能大模型推理引擎，支持连续批处理与 PagedAttention，
可显著降低 KV cache 内存开销并提升吞吐。LoRA 是一种低秩适配的微调方法，
只需训练少量参数即可适配下游任务。

第三段：评估大模型常用 GSM8K、C-Eval、CMMLU 等公开榜单，
也支持基于业务测试集的规则匹配与 LLM-as-a-Judge 评估方式。
"""


def test_parse_md_real(admin_tok, dctx):
    files = {"file": ("doc.md", DOC_MD.encode("utf-8"), "text/markdown")}
    r = requests.post(f"{BASE}/api/datasets/parse",
                      headers=H(admin_tok), timeout=60,
                      params={"dataset_id": dctx["alpaca_ds"], "as_sft": "true"},
                      files=files)
    assert r.status_code == 200, r.text
    t = _wait_task(admin_tok, r.json()["task_id"], timeout=600)
    assert t["status"] == "done", f"parse 失败: {t['error']}"
    assert t["result"]["samples"] >= 1
    vers = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions",
                        headers=H(admin_tok), timeout=15).json()
    out = next(v for v in vers if v["id"] == t["output_version_id"])
    assert out["sample_count"] >= 1
    # 原始文档可读
    r = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions/{out['id']}/raw",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200 and "VerveDeck" in r.text


def test_parse_md_as_sft_false(admin_tok, dctx):
    files = {"file": ("doc2.md", DOC_MD.encode("utf-8"), "text/markdown")}
    r = requests.post(f"{BASE}/api/datasets/parse",
                      headers=H(admin_tok), timeout=60,
                      params={"dataset_id": dctx["alpaca_ds"], "as_sft": "false"},
                      files=files)
    assert r.status_code == 200, r.text
    t = _wait_task(admin_tok, r.json()["task_id"], timeout=600)
    assert t["status"] == "done", f"parse 失败: {t['error']}"
    vers = requests.get(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/versions",
                        headers=H(admin_tok), timeout=15).json()
    out = next(v for v in vers if v["id"] == t["output_version_id"])
    assert out["sample_count"] == 1  # as_sft=false → 单条兜底样本


def test_parse_missing_file_422(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/parse",
                      headers=H(admin_tok), timeout=30,
                      params={"dataset_id": dctx["alpaca_ds"]}, files={})
    assert r.status_code in (400, 422)


def test_parse_missing_ds_404(admin_tok):
    files = {"file": ("x.md", b"# hi", "text/markdown")}
    r = requests.post(f"{BASE}/api/datasets/parse",
                      headers=H(admin_tok), timeout=30,
                      params={"dataset_id": 999999}, files=files)
    assert r.status_code == 404


# ---------- Argilla 标注 ----------

def test_annotate(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/annotate",
                      headers=H(admin_tok), timeout=180,
                      json={"version_id": dctx["alpaca_ver"]})
    assert r.status_code == 200, r.text
    assert r.json()["samples"] == len(ALPACA_ROWS)
    assert r.json()["argilla_url"]


def test_annotate_bad_version_404(admin_tok, dctx):
    r = requests.post(f"{BASE}/api/datasets/{dctx['alpaca_ds']}/annotate",
                      headers=H(admin_tok), timeout=30,
                      json={"version_id": 999999})
    assert r.status_code == 404

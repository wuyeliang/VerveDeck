# -*- coding: utf-8 -*-
"""对象存储（rustfs / S3）集成测试

覆盖：
- dashboard 汇总中的 storage 健康字段
- storage 库 上传/下载 roundtrip（含本地缓存未命中回源）
- object 型数据集版本文件仅存桶（端到端写路径，单副本语义；
  默认数据集已改为仅本地 /data，不再镜像主桶，见 test_api_datasets）
- 删除数据集时 S3 前缀一并清理（force，不依赖本地目录存在）

S3 未配置（S3_ENDPOINT 为空，纯本地模式）时整组 skip。
运行环境：backend 容器内（与 500 套件一致，rustfs 经内网可达）。
"""
import json
import os

import pytest
import requests

from conftest import BASE, H, SUF, DEFAULT_PROJECT


def _s3_enabled() -> bool:
    return bool(os.environ.get("S3_ENDPOINT", ""))


def _minio_client():
    from minio import Minio
    # minio 库签名：Minio("host[:port]", access_key=..., secret_key=..., secure=...)
    # 注意 endpoint 不能带 scheme（内部会再拼 http://，重复 scheme 会报 path in endpoint is not allowed）
    url = os.environ.get("S3_ENDPOINT", "")
    if "://" in url:
        secure = url.startswith("https")
        host = url.split("//", 1)[1].split("/", 1)[0]
    else:
        secure, host = False, (url or "rustfs:9000")
    return Minio(
        host,
        access_key=os.environ.get("S3_ACCESS_KEY", "rustfsadmin"),
        secret_key=os.environ.get("S3_SECRET_KEY", "rustfsadmin"),
        secure=secure,
    )


@pytest.fixture(scope="module")
def s3():
    if not _s3_enabled():
        pytest.skip("S3 未启用（纯本地模式）")
    c = _minio_client()
    bucket = os.environ.get("S3_BUCKET", "vervedeck-datasets")
    if not c.bucket_exists(bucket):
        pytest.skip(f"桶 {bucket} 不存在（rustfs 未就绪？）")
    return {"c": c, "bucket": bucket}


# ---------- dashboard 健康字段 ----------

def test_summary_storage_field(s3, admin_tok):
    s = requests.get(f"{BASE}/api/dashboard/summary", headers=H(admin_tok), timeout=15).json()
    assert "storage" in s
    st = s["storage"]
    assert st["enabled"] is True and st["ok"] is True
    assert st["bucket"] == s3["bucket"]


# ---------- storage 库 roundtrip ----------

def test_put_get_roundtrip(s3):
    import sys
    if "/app" not in sys.path:
        sys.path.insert(0, "/app")
    from app.services import storage
    data_dir = os.environ.get("DATA_DIR", "/data")
    p = os.path.join(data_dir, "s3-selftest", f"{SUF}.jsonl")
    payload = '{"instruction": "s3 roundtrip", "output": "ok"}'
    os.makedirs(os.path.dirname(p), exist_ok=True)
    try:
        with open(p, "w", encoding="utf-8") as f:
            f.write(payload)
        assert storage.put_file(p) is True
        # 本地删除 → 模拟缓存未命中 → 从 S3 回源
        os.remove(p)
        assert storage.ensure_local(p) is True
        with open(p, encoding="utf-8") as f:
            assert f.read() == payload
    finally:
        if os.path.exists(p):
            os.remove(p)
        key = "s3-selftest/" + f"{SUF}.jsonl"
        if _exists(s3, key):
            s3["c"].remove_object(s3["bucket"], key)
        # 清理空目录
        import shutil
        shutil.rmtree(os.path.join(data_dir, "s3-selftest"), ignore_errors=True)


# ---------- 数据集版本端到端同步 ----------

@pytest.fixture()
def s3_dataset(s3, admin_tok):
    """单副本语义：默认数据集仅本地不写桶，端到端桶写路径改用 object 型后端
    （指向平台主对象存储的同一桶，key 布局与主桶一致）"""
    url = os.environ.get("S3_ENDPOINT", "")
    host = url.split("//", 1)[1].split("/", 1)[0] if "://" in url else (url or "rustfs:9000")
    r = requests.post(f"{BASE}/api/storage/backends", headers=H(admin_tok), timeout=60,
                      json={"name": f"s3obj-{SUF}", "type": "object",
                            "config": {"endpoint": host,
                                       "access_key": os.environ.get("S3_ACCESS_KEY", "rustfsadmin"),
                                       "secret_key": os.environ.get("S3_SECRET_KEY", "rustfsadmin"),
                                       "bucket": s3["bucket"]}})
    r.raise_for_status()
    bid = r.json()["id"]
    name = f"s3ds-{SUF}"
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name, "description": "s3 sync test",
                            "storage_backend_id": bid})
    r.raise_for_status()
    dsid = r.json()["id"]
    yield {"id": dsid, "name": name}
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)
    requests.delete(f"{BASE}/api/storage/backends/{bid}", headers=H(admin_tok), timeout=30)


def _vkey(project_id: int, name: str, version: int, fname: str) -> str:
    import re
    safe = re.sub(r"[^\w\-]", "_", name)
    return f"datasets/p{project_id}/{safe}/v{version}/{fname}"


def _exists(s3ctx, key: str) -> bool:
    """stat_object：对象不存在会抛 S3Error，统一转为 False"""
    try:
        return s3ctx["c"].stat_object(s3ctx["bucket"], key) is not None
    except Exception:
        return False


def test_version_uploaded_to_s3(s3, s3_dataset, admin_tok):
    rows = [{"instruction": f"问题 {i}", "input": "", "output": f"回答 {i}"} for i in range(3)]
    content = "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)
    r = requests.post(
        f"{BASE}/api/datasets/{s3_dataset['id']}/versions/upload",
        headers=H(admin_tok), timeout=30,
        data={"fmt": "alpaca", "note": "s3-upload"},
        files={"file": (f"s3-{SUF}.jsonl", content.encode(), "application/jsonl")},
    )
    r.raise_for_status()
    ver = r.json()
    key = _vkey(DEFAULT_PROJECT, s3_dataset["name"], ver["version"], "data.jsonl")
    assert _exists(s3, key), f"S3 缺少对象 {key}"


def test_delete_dataset_removes_s3_prefix(s3, s3_dataset, admin_tok):
    rows = [{"instruction": "del", "input": "", "output": "x"}]
    content = json.dumps(rows[0], ensure_ascii=False)
    r = requests.post(
        f"{BASE}/api/datasets/{s3_dataset['id']}/versions/upload",
        headers=H(admin_tok), timeout=30,
        data={"fmt": "alpaca", "note": "s3-del"},
        files={"file": (f"s3del-{SUF}.jsonl", content.encode(), "application/jsonl")},
    )
    r.raise_for_status()
    ver = r.json()
    key = _vkey(DEFAULT_PROJECT, s3_dataset["name"], ver["version"], "data.jsonl")
    assert _exists(s3, key)
    # 删除数据集 → S3 前缀清理
    r = requests.delete(f"{BASE}/api/datasets/{s3_dataset['id']}", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200, r.text
    assert not _exists(s3, key), "删除数据集后 S3 对象应已清理"


# ---------------------------------------------------------------------------
# 存储后端注册：JuiceFS（FUSE 挂载）+ 存储位置选择
#
# 探测与断言均自适应环境：/data/juicefs 是真实 FUSE 挂载时 status=ok，
# 未挂载（或挂载未传播到本容器命名空间）时 status=error——注册本身恒成功。
# ---------------------------------------------------------------------------

JFS_PATH = "/data/juicefs"


def _is_fuse_mount(path: str) -> bool:
    """/proc/mounts 判定 path 是否为 FUSE 挂载（与 probe_juicefs 的 df -T 视图同源）"""
    try:
        with open("/proc/mounts") as f:
            for line in f:
                f_ = line.split()
                if len(f_) >= 3 and f_[1] == path and "fuse" in f_[2].lower():
                    return True
    except OSError:
        pass
    return False


def _mk_jfs_backend(tok: str, name: str) -> dict:
    r = requests.post(f"{BASE}/api/storage/backends", headers=H(tok), timeout=60,
                      json={"name": name, "type": "juicefs",
                            "config": {"path": JFS_PATH, "volume": "vervedeck",
                                       "meta_url": "redis://redis:6379/1"}})
    r.raise_for_status()
    return r.json()


@pytest.fixture()
def jfs_backend(admin_tok):
    b = _mk_jfs_backend(admin_tok, f"jfs-{SUF}")
    yield {"id": b["id"], "name": b["name"], "status": b["status"]}
    requests.delete(f"{BASE}/api/storage/backends/{b['id']}", headers=H(admin_tok), timeout=30)


# ---------- 注册校验 ----------

def test_juicefs_missing_path_400(admin_tok):
    r = requests.post(f"{BASE}/api/storage/backends", headers=H(admin_tok), timeout=30,
                      json={"name": f"jfsnp-{SUF}", "type": "juicefs", "config": {}})
    assert r.status_code == 400
    assert "path" in r.json()["detail"]


def test_juicefs_relative_path_400(admin_tok):
    r = requests.post(f"{BASE}/api/storage/backends", headers=H(admin_tok), timeout=30,
                      json={"name": f"jfsrp-{SUF}", "type": "juicefs",
                            "config": {"path": "data/juicefs"}})
    assert r.status_code == 400
    assert "绝对路径" in r.json()["detail"]


def test_juicefs_unknown_type_400(admin_tok):
    r = requests.post(f"{BASE}/api/storage/backends", headers=H(admin_tok), timeout=30,
                      json={"name": f"jfsu-{SUF}", "type": "cos", "config": {"path": "/x"}})
    assert r.status_code == 400


def test_juicefs_duplicate_name_400(admin_tok, jfs_backend):
    r = requests.post(f"{BASE}/api/storage/backends", headers=H(admin_tok), timeout=30,
                      json={"name": jfs_backend["name"], "type": "juicefs",
                            "config": {"path": JFS_PATH}})
    assert r.status_code == 400
    assert "已存在" in r.json()["detail"]


def test_juicefs_register_status_matches_mount(admin_tok):
    """注册恒成功；status 与 /data/juicefs 的实际 FUSE 挂载状态一致"""
    b = _mk_jfs_backend(admin_tok, f"jfsst-{SUF}")
    try:
        assert b["type"] == "juicefs"
        if _is_fuse_mount(JFS_PATH):
            assert b["status"] == "ok", f"FUSE 挂载存在但探测未 ok: {b}"
            assert b["size_total"] > 0
        else:
            assert b["status"] == "error", f"无 FUSE 挂载但探测误报 ok: {b}"
            assert "FUSE" in b["detail"]
    finally:
        requests.delete(f"{BASE}/api/storage/backends/{b['id']}", headers=H(admin_tok), timeout=30)


# ---------- options 端点（下拉数据源） ----------

def test_backends_options_shape_no_credentials(admin_tok):
    r = requests.get(f"{BASE}/api/storage/backends/options", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    rows = r.json()
    assert isinstance(rows, list)
    for row in rows:
        assert set(row.keys()) == {"id", "name", "type", "status"}
        assert "config" not in row  # 不泄露 endpoint/密钥等凭证


def test_backends_options_requires_login():
    r = requests.get(f"{BASE}/api/storage/backends/options", timeout=15)
    assert r.status_code in (401, 403)


def test_backends_options_dev_readable(roles):
    """任意登录用户可读（数据集/模型创建页下拉），developer 即可"""
    r = requests.get(f"{BASE}/api/storage/backends/options",
                     headers=H(roles["developer"]), timeout=15)
    assert r.status_code == 200
    assert isinstance(r.json(), list)


# ---------- 存储位置选择：数据集引用 + 删除引用检查 ----------

def test_juicefs_backend_lifecycle_and_refcheck(admin_tok, jfs_backend):
    bid, bname = jfs_backend["id"], jfs_backend["name"]
    dsname = f"jfsds-{SUF}"
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": dsname,
                            "storage_backend_id": bid})
    assert r.status_code == 200, r.text
    dsid = r.json()["id"]
    try:
        rows = requests.get(f"{BASE}/api/datasets", params={"project_id": DEFAULT_PROJECT},
                            headers=H(admin_tok), timeout=15).json()
        d = next(x for x in rows if x["id"] == dsid)
        assert d["storage_backend_id"] == bid
        assert bname in d["storage_backend"]
        # 详情同字段
        det = requests.get(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=15).json()
        assert det["storage_backend_id"] == bid
        # 被数据集引用 → 后端不可删除
        r = requests.delete(f"{BASE}/api/storage/backends/{bid}", headers=H(admin_tok), timeout=30)
        assert r.status_code == 400
        assert "引用" in r.json()["detail"]
    finally:
        requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)
    # 引用解除后可删除
    r = requests.delete(f"{BASE}/api/storage/backends/{bid}", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200


def test_dataset_unknown_backend_400(admin_tok):
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": f"jfsbad-{SUF}",
                            "storage_backend_id": 99999999})
    assert r.status_code == 400
    assert "不存在" in r.json()["detail"]

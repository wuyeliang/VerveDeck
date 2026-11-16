# -*- coding: utf-8 -*-
"""对象存储（rustfs / S3）集成测试

覆盖：
- dashboard 汇总中的 storage 健康字段
- storage 库 上传/下载 roundtrip（含本地缓存未命中回源）
- 数据集版本 commit 后自动同步到 S3（端到端写路径）
- 删除数据集时 S3 前缀一并清理

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
    name = f"s3ds-{SUF}"
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": DEFAULT_PROJECT, "name": name, "description": "s3 sync test"})
    r.raise_for_status()
    dsid = r.json()["id"]
    yield {"id": dsid, "name": name}
    requests.delete(f"{BASE}/api/datasets/{dsid}", headers=H(admin_tok), timeout=30)


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

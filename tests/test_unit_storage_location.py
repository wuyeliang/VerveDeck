# -*- coding: utf-8 -*-
"""多存储位置单测：probe_juicefs 解析 / test_backend 分发 / 桶解析 / 位置助手 / dataset_repo_dir

docker 全部 mock、DB 用 sqlite 内存库，不触网、不起容器。
本地可跑：PYTHONPATH=platform/backend python3 -m pytest tests/test_unit_storage_location.py --noconftest
"""
import os

import pytest
import docker as docker_mod
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models as M
from app.config import settings
from app.services import storage
from app.services import dataset_service as dsvc


# ---------------- 公共 fixture ----------------

@pytest.fixture()
def sdb(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    # 基础数据：租户 / 用户 / 项目
    t = M.Tenant(name="t")
    s.add(t)
    s.flush()
    u = M.User(username="u", password_hash="x")
    s.add(u)
    s.flush()
    p = M.Project(tenant_id=t.id, name="p", owner_id=u.id)
    s.add(p)
    s.commit()
    yield s, u, p
    s.close()


# ---------------- mock docker（probe_juicefs 一次性容器） ----------------

class _FakeContainer:
    def __init__(self, logs: str, status: int = 0):
        self._logs = logs
        self._status = status
        self.removed = False

    def start(self):
        pass

    def wait(self, timeout=60):
        return {"StatusCode": self._status}

    def logs(self):
        return self._logs.encode("utf-8")

    def remove(self, force=False):
        self.removed = True


class _FakeDocker:
    def __init__(self, logs: str, status: int = 0):
        self._ctr = _FakeContainer(logs, status)
        self.created_with = None
        outer = self
        ctr = self._ctr

        class _Images:
            def get(self, name):
                return object()

        class _Containers:
            def create(self, image, entrypoint=None, volumes=None):
                outer.created_with = {
                    "image": image, "entrypoint": entrypoint, "volumes": volumes,
                }
                return ctr

        self.images = _Images()
        self.containers = _Containers()


def _df_line(fstype: str) -> str:
    """df -kPT 输出：Filesystem Type 1024-blocks Used Available Capacity Mounted-on"""
    return f"fs {fstype} 1073741824 524288 1073217536 1% /probe/juicefs"


def _patch_docker(monkeypatch, logs: str, status: int = 0) -> _FakeDocker:
    fake = _FakeDocker(logs, status)
    monkeypatch.setattr(docker_mod, "from_env", lambda: fake)
    return fake


JFS_CFG = {"path": "/data/juicefs", "volume": "vervedeck", "meta_url": "redis://redis:6379/1"}


# ---------------- probe_juicefs ----------------

def test_probe_juicefs_relative_path_rejected():
    r = storage.probe_juicefs({"path": "data/juicefs"})
    assert r["ok"] is False
    assert "绝对路径" in r["detail"]


def test_probe_juicefs_missing_path():
    r = storage.probe_juicefs({})
    assert r["ok"] is False
    assert "绝对路径" in r["detail"]


def test_probe_juicefs_fuse_ok(monkeypatch):
    _patch_docker(monkeypatch, _df_line("fuse"))
    r = storage.probe_juicefs(JFS_CFG)
    assert r["ok"] is True
    assert "FUSE 挂载正常" in r["detail"]
    assert r["size_total"] == 1073741824 * 1024
    assert r["size_used"] == (1073741824 - 1073217536) * 1024


def test_probe_juicefs_fuse_juicefs_fstype(monkeypatch):
    """juicefs 实际 fstype 常为 fuse.juicefs，子串匹配需通过"""
    _patch_docker(monkeypatch, _df_line("fuse.juicefs"))
    r = storage.probe_juicefs(JFS_CFG)
    assert r["ok"] is True


def test_probe_juicefs_non_fuse_rejected(monkeypatch):
    _patch_docker(monkeypatch, _df_line("ext4"))
    r = storage.probe_juicefs(JFS_CFG)
    assert r["ok"] is False
    assert "不是 FUSE 挂载" in r["detail"]
    assert "ext4" in r["detail"]


def test_probe_juicefs_mountpoint_not_exists(monkeypatch):
    """容器内 test -d 失败（exit 1）→ 挂载点不存在"""
    _patch_docker(monkeypatch, "", status=1)
    r = storage.probe_juicefs(JFS_CFG)
    assert r["ok"] is False
    assert "不存在或不可读" in r["detail"]


def test_probe_juicefs_malformed_df_output(monkeypatch):
    _patch_docker(monkeypatch, "garbage")
    r = storage.probe_juicefs(JFS_CFG)
    assert r["ok"] is False
    assert r["detail"] == "garbage"


def test_probe_juicefs_docker_error(monkeypatch):
    monkeypatch.setattr(docker_mod, "from_env", lambda: (_ for _ in ()).throw(RuntimeError("docker down")))
    r = storage.probe_juicefs(JFS_CFG)
    assert r["ok"] is False
    assert "docker down" in r["detail"]


def test_probe_juicefs_binds_mountpoint_directly(monkeypatch):
    """必须直接 bind 挂载点本身：rprivate 不传播宿主子挂载，挂父目录看不到 FUSE"""
    fake = _patch_docker(monkeypatch, _df_line("fuse"))
    storage.probe_juicefs(JFS_CFG)
    cw = fake.created_with
    assert "df -kPT" in cw["entrypoint"][2]
    assert cw["volumes"] == {"/data/juicefs": {"bind": "/probe/juicefs", "mode": "ro"}}
    assert fake._ctr.removed is True  # 容器用完即删


# ---------------- test_backend 分发 ----------------

def test_test_backend_juicefs_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(storage, "probe_juicefs",
                        lambda cfg: calls.append(cfg) or {"ok": True, "detail": "mock"})
    r = storage.test_backend("juicefs", JFS_CFG)
    assert calls == [JFS_CFG]
    assert r["ok"] is True
    assert r["size_used"] == 0 and r["size_total"] == 0  # setdefault 兜底


def test_test_backend_object_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(storage, "probe_object",
                        lambda cfg: calls.append(cfg) or {"ok": False, "detail": "bucket 不存在"})
    r = storage.test_backend("object", {"bucket": "b"})
    assert calls == [{"bucket": "b"}]
    assert r["ok"] is False


@pytest.mark.parametrize("btype", ["local", "nfs", "block"])
def test_test_backend_path_types_require_path(btype):
    r = storage.test_backend(btype, {})
    assert r["ok"] is False
    assert "path 必填" in r["detail"]


def test_test_backend_path_dispatch(monkeypatch):
    calls = []
    monkeypatch.setattr(storage, "probe_path",
                        lambda p: calls.append(p) or {"ok": True, "detail": "ok"})
    r = storage.test_backend("nfs", {"path": "/mnt/nfs1"})
    assert calls == ["/mnt/nfs1"]
    assert r["ok"] is True


# ---------------- _bucket_for ----------------

def test_bucket_for_none_disabled():
    settings.s3_endpoint = ""
    try:
        assert storage._bucket_for(None) == (None, None)
        # 空 cfg 视同未指定（falsy），同样走主存储降级
        assert storage._bucket_for({}) == (None, None)
    finally:
        settings.s3_endpoint = ""


def test_bucket_for_none_enabled(monkeypatch):
    monkeypatch.setattr(settings, "s3_endpoint", "http://rustfs:9000")
    monkeypatch.setattr(storage, "ensure_bucket", lambda: None)
    monkeypatch.setattr(storage, "client", lambda: "FAKE_MAIN_CLIENT")
    try:
        c, b = storage._bucket_for(None)
        assert c == "FAKE_MAIN_CLIENT" and b == settings.s3_bucket
    finally:
        settings.s3_endpoint = ""


class _FakeBackendClient:
    def __init__(self, exists: bool = True):
        self.exists = exists
        self.made = []

    def bucket_exists(self, b):
        return self.exists

    def make_bucket(self, b):
        self.made.append(b)


def test_bucket_for_cfg_existing_bucket(monkeypatch):
    fake = _FakeBackendClient(exists=True)
    monkeypatch.setattr(storage, "_minio_client", lambda *a, **k: fake)
    cfg = {"endpoint": "http://h:9000", "bucket": "b2"}
    c, b = storage._bucket_for(cfg)
    assert c is fake and b == "b2"
    assert fake.made == []


def test_bucket_for_cfg_creates_missing_bucket(monkeypatch):
    fake = _FakeBackendClient(exists=False)
    monkeypatch.setattr(storage, "_minio_client", lambda *a, **k: fake)
    c, b = storage._bucket_for({"endpoint": "e", "bucket": "b3"})
    assert fake.made == ["b3"]


def test_bucket_for_cfg_missing_bucket_raises(monkeypatch):
    monkeypatch.setattr(storage, "_minio_client", lambda *a, **k: _FakeBackendClient())
    with pytest.raises(ValueError, match="bucket"):
        storage._bucket_for({"endpoint": "e"})


# ---------------- backend_root / backend_subdir ----------------

def _b(bid: int, type_: str, cfg: dict | None):
    return M.StorageBackend(id=bid, name=f"b{bid}", type=type_, config=cfg or {})


def test_backend_root_strips_trailing_slash():
    assert storage.backend_root(_b(1, "juicefs", {"path": "/data/juicefs/"})) == "/data/juicefs"
    assert storage.backend_root(_b(2, "local", None)) == ""
    assert storage.backend_root(_b(3, "nfs", {"path": ""})) == ""


def test_backend_subdir_join():
    b = _b(1, "juicefs", {"path": "/mnt/jfs"})
    assert storage.backend_subdir(b, "datasets", "p7", "demo") == "/mnt/jfs/datasets/p7/demo"


def test_backend_subdir_no_root_raises():
    with pytest.raises(ValueError, match="path"):
        storage.backend_subdir(_b(9, "juicefs", {}), "datasets")


# ---------------- dataset_repo_dir / _object_cfg ----------------

def _dset(sdb, user, proj, name="demo/data", backend=None):
    d = M.Dataset(project_id=proj.id, name=name, created_by=user.id,
                  storage_backend_id=backend.id if backend else None)
    sdb.add(d)
    sdb.commit()
    return d


def test_repo_dir_default(sdb):
    s, u, p = sdb
    d = _dset(s, u, p)
    repo = dsvc.dataset_repo_dir(p.id, d.name, d, s)
    assert repo == os.path.join(settings.data_dir, "datasets", f"p{p.id}", "demo_data")
    # 未传 dset/db 时行为完全不变
    assert dsvc.dataset_repo_dir(p.id, "demo/data") == repo


def test_repo_dir_juicefs_backend(sdb):
    s, u, p = sdb
    b = M.StorageBackend(name="jfs-b", type="juicefs", config={"path": "/mnt/jfs"})
    s.add(b)
    s.commit()
    d = _dset(s, u, p, backend=b)
    repo = dsvc.dataset_repo_dir(p.id, d.name, d, s)
    assert repo == os.path.join("/mnt/jfs", "datasets", f"p{p.id}", "demo_data")


def test_repo_dir_path_backends(sdb):
    s, u, p = sdb
    for t in ("local", "nfs", "block"):
        b = M.StorageBackend(name=f"{t}-b", type=t, config={"path": f"/mnt/{t}"})
        s.add(b)
        s.commit()
        d = _dset(s, u, p, name=f"n-{t}", backend=b)
        # 连字符在合法字符集内，不会被转义
        assert dsvc.dataset_repo_dir(p.id, d.name, d, s) == \
            f"/mnt/{t}/datasets/p{p.id}/n-{t}"


def test_repo_dir_object_backend_stays_default(sdb):
    """object 型：本地仓库仍在 data_dir，同步方向由 _object_cfg 决定"""
    s, u, p = sdb
    b = M.StorageBackend(name="obj-b", type="object",
                         config={"endpoint": "http://x:9000", "bucket": "ob"})
    s.add(b)
    s.commit()
    d = _dset(s, u, p, backend=b)
    repo = dsvc.dataset_repo_dir(p.id, d.name, d, s)
    assert repo == os.path.join(settings.data_dir, "datasets", f"p{p.id}", "demo_data")
    assert dsvc._object_cfg(d, s) == b.config


def test_repo_dir_dangling_backend_falls_back(sdb):
    """后端已被删除（引用悬空）→ 按平台默认处理，不报错"""
    s, u, p = sdb
    b = M.StorageBackend(name="gone", type="juicefs", config={"path": "/mnt/gone"})
    s.add(b)
    s.commit()
    d = _dset(s, u, p, backend=b)
    s.delete(b)
    s.commit()
    repo = dsvc.dataset_repo_dir(p.id, d.name, d, s)
    assert repo == os.path.join(settings.data_dir, "datasets", f"p{p.id}", "demo_data")
    assert dsvc._object_cfg(d, s) is None


def test_object_cfg_only_for_object_type(sdb):
    s, u, p = sdb
    b = M.StorageBackend(name="jfs2", type="juicefs", config={"path": "/mnt/j2"})
    s.add(b)
    s.commit()
    d = _dset(s, u, p, backend=b)
    assert dsvc._object_cfg(d, s) is None  # path 型不同步对象存储
    assert dsvc._object_cfg(None, s) is None
    assert dsvc._object_cfg(d, None) is None


# ---------------- worker 容器存储挂载（taskcontainer._storage_extra_paths） ----------------

def test_storage_extra_paths_binds_submount_in_datadir(tmp_path, monkeypatch):
    """path 型后端根一律同路径 bind（含 /data 内的 FUSE 子挂载）：
    容器 /data 的 rprivate bind 不传播宿主子挂载（实测 FUSE 挂载点在容器内为空目录）"""
    import app.database as dbmod
    from app.services import taskcontainer as tc
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    monkeypatch.setattr(dbmod, "SessionLocal", S)
    inner = tmp_path / "jfs"
    inner.mkdir()
    s.add(M.StorageBackend(name="jfs-x", type="juicefs", config={"path": str(inner)}))
    s.add(M.StorageBackend(name="jfs-miss", type="juicefs", config={"path": "/nonexistent-xyz"}))
    s.add(M.StorageBackend(name="obj-x", type="object", config={"bucket": "b"}))
    s.commit()
    vols = tc._storage_extra_paths()
    # data_dir 内的挂载点也必须 bind（旧逻辑误以为被主 /data 挂载覆盖）
    assert vols.get(str(inner)) == {"bind": str(inner), "mode": "rw"}
    # 宿主不存在的路径不挂；object 型不在列
    assert "/nonexistent-xyz" not in vols
    assert len(vols) == 1
    s.close()


# ---------------- ensure_version_local 传参链 ----------------

def test_ensure_version_local_uses_object_cfg(sdb, monkeypatch):
    s, u, p = sdb
    b = M.StorageBackend(name="obj-c", type="object",
                         config={"endpoint": "http://x:9000", "bucket": "ob"})
    s.add(b)
    s.commit()
    d = _dset(s, u, p, backend=b)
    repo = dsvc.dataset_repo_dir(p.id, d.name, d, s)
    pth = dsvc.version_data_file(repo, 1)
    cfgs = []
    monkeypatch.setattr(storage, "ensure_local", lambda p, cfg=None: cfgs.append(cfg) or False)
    dsvc.ensure_version_local(repo, 1, d, s)
    assert cfgs == [b.config]
    # path 型后端不传 cfg（无同步）
    b2 = M.StorageBackend(name="jfs3", type="juicefs", config={"path": "/mnt/j3"})
    s.add(b2)
    s.commit()
    d2 = _dset(s, u, p, name="n2", backend=b2)
    repo2 = dsvc.dataset_repo_dir(p.id, d2.name, d2, s)
    cfgs.clear()
    monkeypatch.setattr(storage, "ensure_local", lambda p, cfg=None: cfgs.append(cfg) or False)
    dsvc.ensure_version_local(repo2, 1, d2, s)
    assert cfgs == [None]

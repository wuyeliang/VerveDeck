"""训练任务批量删除单测（#97 补漏）：sqlite 内存库 + 直接调端点函数，不起服务器。

覆盖：全删 / 排队与运行中拒绝 / 已产资产保护 / 续训依赖保护 / 不存在 / 跨项目 403 / 磁盘清理。
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Tenant, User, Project, TrainingJob, ModelAsset
from app.api import training as tr


@pytest.fixture()
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    s = sessionmaker(bind=engine)()
    yield s
    s.close()


@pytest.fixture()
def seed(db):
    """租户 + 超管 + 两个项目（跨项目 403 用）"""
    t = Tenant(name="t1")
    db.add(t); db.commit(); db.refresh(t)
    boss = User(username="boss", password_hash="x", role="super_admin", tenant_id=t.id)
    dev = User(username="dev", password_hash="x", role="developer", tenant_id=t.id)
    db.add_all([boss, dev]); db.commit(); db.refresh(boss); db.refresh(dev)
    p1 = Project(tenant_id=t.id, name="p1", owner_id=boss.id)
    p2 = Project(tenant_id=t.id, name="p2", owner_id=boss.id)
    db.add_all([p1, p2]); db.commit(); db.refresh(p1); db.refresh(p2)
    return {"tenant": t, "boss": boss, "dev": dev, "p1": p1, "p2": p2}


def _job(db, pid, status="succeeded", name="j", work_dir="", **kw):
    j = TrainingJob(project_id=pid, name=name, stage="sft", status=status,
                    submitted_by=1, work_dir=work_dir, **kw)
    db.add(j); db.commit(); db.refresh(j)
    return j


def test_batch_delete_all_deletable(db, seed):
    a = _job(db, seed["p1"].id, "succeeded", "a")
    b = _job(db, seed["p1"].id, "failed", "b")
    c = _job(db, seed["p1"].id, "cancelled", "c")
    r = tr.batch_delete_jobs(tr.BatchDeleteReq(ids=[a.id, b.id, c.id]), db=db, user=seed["boss"])
    assert sorted(r["deleted"]) == sorted([a.id, b.id, c.id]) and r["failed"] == []
    assert db.query(TrainingJob).count() == 0


def test_batch_delete_running_and_queued_rejected(db, seed):
    ok = _job(db, seed["p1"].id, "succeeded", "ok")
    q = _job(db, seed["p1"].id, "queued", "q")
    r = _job(db, seed["p1"].id, "running", "r")
    res = tr.batch_delete_jobs(tr.BatchDeleteReq(ids=[ok.id, q.id, r.id]), db=db, user=seed["boss"])
    assert res["deleted"] == [ok.id]
    errs = {f["id"]: f["error"] for f in res["failed"]}
    assert set(errs) == {q.id, r.id}
    assert "不可删除" in errs[q.id] and "不可删除" in errs[r.id]
    # 失败任务仍在库中（部分失败不影响其他项）
    assert db.query(TrainingJob).count() == 2


def test_batch_delete_asset_protection(db, seed):
    j = _job(db, seed["p1"].id, "succeeded", "with-asset")
    asset = ModelAsset(project_id=seed["p1"].id, name="merged-a", type="merged",
                       job_id=j.id, created_by=seed["boss"].id)
    db.add(asset); db.commit()
    res = tr.batch_delete_jobs(tr.BatchDeleteReq(ids=[j.id]), db=db, user=seed["boss"])
    assert res["deleted"] == [] and "模型资产" in res["failed"][0]["error"]
    assert db.get(TrainingJob, j.id) is not None


def test_batch_delete_resume_dependency_protection(db, seed):
    parent = _job(db, seed["p1"].id, "succeeded", "parent")
    child = _job(db, seed["p1"].id, "queued", "child", resume_from_job_id=parent.id)
    res = tr.batch_delete_jobs(tr.BatchDeleteReq(ids=[parent.id]), db=db, user=seed["boss"])
    assert res["deleted"] == [] and "续训" in res["failed"][0]["error"]
    assert db.get(TrainingJob, parent.id) is not None
    assert db.get(TrainingJob, child.id) is not None


def test_batch_delete_missing_and_other_project(db, seed):
    mine = _job(db, seed["p1"].id, "succeeded", "mine")
    other = _job(db, seed["p2"].id, "succeeded", "other")
    # 超管全项目可见可删；普通 dev 未加入 p2 → 403
    res = tr.batch_delete_jobs(tr.BatchDeleteReq(ids=[mine.id, other.id, 99999]),
                               db=db, user=seed["boss"])
    assert sorted(res["deleted"]) == sorted([mine.id, other.id])
    assert {f["id"] for f in res["failed"]} == {99999} and "不存在" in res["failed"][0]["error"]
    res2 = tr.batch_delete_jobs(tr.BatchDeleteReq(ids=[99999]), db=db, user=seed["dev"])
    assert res2["deleted"] == [] and "不存在" in res2["failed"][0]["error"]


def test_batch_delete_work_dir_cleanup(db, seed, monkeypatch):
    import shutil, tempfile
    root = tempfile.mkdtemp(prefix="vd-train-batch-")  # 不走 tmp_path：沙箱 shim 对 makedirs(exist_ok) 不稳定
    try:
        wd = os.path.join(root, "jobwork")
        os.makedirs(wd)
        with open(os.path.join(wd, "run.log"), "w") as f:
            f.write("x")
        monkeypatch.setattr(tr.settings, "data_dir", root)
        j = _job(db, seed["p1"].id, "succeeded", "with-dir", work_dir=wd)
        res = tr.batch_delete_jobs(tr.BatchDeleteReq(ids=[j.id]), db=db, user=seed["boss"])
        assert res["deleted"] == [j.id]
        assert not os.path.exists(wd)  # 任务工作目录随删除清理
    finally:
        shutil.rmtree(root, ignore_errors=True)


def test_batch_delete_work_dir_outside_data_dir_kept(db, seed, monkeypatch):
    # 数据目录外的 work_dir 不动（与单删语义一致：仅限平台数据目录内清理）
    import tempfile
    root = tempfile.mkdtemp(prefix="vd-train-batch-out-")
    try:
        wd = os.path.join(root, "outside")
        os.makedirs(wd)
        other_root = os.path.join(root, "root")
        os.makedirs(other_root)
        monkeypatch.setattr(tr.settings, "data_dir", other_root)
        j = _job(db, seed["p1"].id, "succeeded", "outside-dir", work_dir=wd)
        res = tr.batch_delete_jobs(tr.BatchDeleteReq(ids=[j.id]), db=db, user=seed["boss"])
        assert res["deleted"] == [j.id]
        assert os.path.exists(wd)
    finally:
        import shutil
        shutil.rmtree(root, ignore_errors=True)

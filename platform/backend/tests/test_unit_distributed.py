# -*- coding: utf-8 -*-
"""分布式训练编排单测（任务 #60：多机多卡/单机多卡）

覆盖：
- training_cmd：单卡回退（与旧版字节级一致）/ 单机多卡 / 多机多机 torchrun
- container_env：NCCL 默认注入（多卡 P2P_DISABLE）+ IB/RDMA 用户覆盖优先级
- rank 命名 / 日志路径 / master_port / job_gpu_need（含旧任务兼容）
- build_config：FSDP 分支（fsdp / fsdp auto_wrap）、none 不写额外配置
- launch：docker 全 mock 的编排（容器数/GPU 绑定/env/每 rank 命令/DB 收敛）
- scheduler：跨节点准入 _free_gpus/_admit_jobs、多 rank 收敛 _reconcile_training

运行环境：本地 sqlite 内存库即可（不依赖 docker/GPU）。
"""
import os

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models as M
from app.services import training_service as ts
from app.scheduler import _free_gpus, _admit_jobs, _reconcile_training
from app import scheduler as sched


class J:
    """轻量 fake job：纯函数测试只需要 id/拓扑/nccl_env 字段"""

    def __init__(self, id=1, node_count=1, nproc_per_node=1, gpu_count=1, nccl_env=None):
        self.id = id
        self.node_count = node_count
        self.nproc_per_node = nproc_per_node
        self.gpu_count = gpu_count
        self.nccl_env = nccl_env or {}


# ---------------- training_cmd：启动命令构造 ----------------

def test_cmd_single_card_fallback_byte_identical():
    """单卡回退：nnodes==1 且 nproc==1 → 原生 llamafactory-cli，与旧版命令完全一致"""
    cmd = ts.training_cmd(J(7), "config.yaml", 0, 1, 1, "127.0.0.1", 29507)
    assert cmd == ["bash", "-lc", "cd /data && llamafactory-cli train config.yaml 2>&1"]


def test_cmd_single_node_multigpu_torchrun():
    cmd = ts.training_cmd(J(7), "config.yaml", 0, 1, 4, "127.0.0.1", 29507)
    assert cmd[0] == "bash" and cmd[1] == "-lc"
    c = cmd[2]
    assert "torchrun --nproc_per_node=4 --nnodes=1 --node_rank=0" in c
    assert "--master_addr=127.0.0.1 --master_port=29507" in c
    assert "-m llamafactory.cli train config.yaml 2>&1" in c
    assert "llamafactory-cli" not in c


def test_cmd_multinode_node_rank_and_master_addr():
    c0 = ts.training_cmd(J(8), "config.yaml", 0, 2, 2, "10.1.2.3", 29508)[2]
    c1 = ts.training_cmd(J(8), "config.yaml", 1, 2, 2, "10.1.2.3", 29508)[2]
    assert "--nnodes=2 --node_rank=0 --master_addr=10.1.2.3" in c0
    assert "--nnodes=2 --node_rank=1 --master_addr=10.1.2.3" in c1
    assert "--nproc_per_node=2" in c0 and "--nproc_per_node=2" in c1


# ---------------- container_env：NCCL / IB-RDMA ----------------

def test_env_single_card_no_nccl():
    """单卡：与旧版 env 完全一致（不注入任何 NCCL 变量）"""
    assert ts.container_env(J(1)) == {"OMP_NUM_THREADS": "4"}


def test_env_single_node_multigpu_p2p_disable():
    env = ts.container_env(J(1, node_count=1, nproc_per_node=4, gpu_count=4))
    assert env["NCCL_P2P_DISABLE"] == "1"      # 4090/PCIe P2P 不稳兜底
    assert "NCCL_DEBUG" not in env             # 单机不强制 INFO
    assert env["OMP_NUM_THREADS"] == "4"


def test_env_multinode_debug_and_shm():
    env = ts.container_env(J(1, node_count=2, nproc_per_node=2, gpu_count=4))
    assert env["NCCL_P2P_DISABLE"] == "1"
    assert env["NCCL_DEBUG"] == "INFO"
    assert env["NCCL_SHM_DISABLE"] == "0"


def test_env_ib_rdma_user_override_wins():
    """IB/RDMA：用户 nccl_env 优先级最高（含 NCCL_P2P_DISABLE=0 反转）"""
    j = J(1, node_count=2, nproc_per_node=2, gpu_count=4, nccl_env={
        "NCCL_IB_HCA": "mlx5_0:1,mlx5_1:1", "NCCL_SOCKET_IFNAME": "eth0",
        "NCCL_NET_GDR_LEVEL": "2", "NCCL_IB_GID_INDEX": 3,
        "NCCL_P2P_DISABLE": "0"})
    env = ts.container_env(j)
    assert env["NCCL_P2P_DISABLE"] == "0"      # 用户覆盖默认
    assert env["NCCL_IB_HCA"] == "mlx5_0:1,mlx5_1:1"
    assert env["NCCL_SOCKET_IFNAME"] == "eth0"
    assert env["NCCL_NET_GDR_LEVEL"] == "2"
    assert env["NCCL_IB_GID_INDEX"] == "3"     # int 值被 stringify
    assert env["NCCL_DEBUG"] == "INFO"         # 平台默认仍保留


# ---------------- rank 命名 / 路径 / 端口 / 卡数 ----------------

def test_rank_names_and_keys():
    j = J(42, node_count=3)
    assert ts.rank_container_names(j) == ["ft-train-42", "ft-train-42-r1", "ft-train-42-r2"]
    assert ts.rank_task_keys(j) == ["train-42", "train-42-r1", "train-42-r2"]
    # 单节点 = 旧命名（回归兼容）
    assert ts.rank_container_names(J(42)) == ["ft-train-42"]
    assert ts.rank_task_keys(J(42)) == ["train-42"]


def test_rank_log_paths(monkeypatch, tmp_path):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    j = J(5, node_count=2)
    base = os.path.join(str(tmp_path), "jobs", "5")
    assert ts.rank_log_paths(j) == [os.path.join(base, "run.log"),
                                    os.path.join(base, "run_r1.log")]


def test_master_port_deterministic_and_distinct():
    assert ts.master_port(J(1)) == 29501
    assert ts.master_port(J(2)) == 29502
    assert ts.master_port(J(2001)) == 29501    # 取模错开同机并发端口
    assert ts.master_port(J(0)) == 29500


def test_job_gpu_need_new_and_legacy():
    assert ts.job_gpu_need(J(1, node_count=2, nproc_per_node=4, gpu_count=8)) == 8
    # 旧任务：node/nproc 缺省 1，但 gpu_count=6（旧单节点多卡写法）→ 取较大值
    assert ts.job_gpu_need(J(1, node_count=1, nproc_per_node=1, gpu_count=6)) == 6
    # gpu_count 被低估（=2）时按 节点×每节点 修正
    assert ts.job_gpu_need(J(1, node_count=2, nproc_per_node=2, gpu_count=2)) == 4
    # 零值容错
    assert ts.job_gpu_need(J(1, node_count=0, nproc_per_node=0, gpu_count=0)) == 1


def test_resolve_master_addr_single_and_multi(monkeypatch):
    assert ts.resolve_master_addr([], 1) == "127.0.0.1"   # 单机不碰 docker
    monkeypatch.setattr(ts.tc, "container_ip", lambda name: "10.9.9.9")
    assert ts.resolve_master_addr(["cid-0", "cid-1"], 2) == "10.9.9.9"


def test_resolve_master_addr_timeout_raises(monkeypatch):
    monkeypatch.setattr(ts.tc, "container_ip", lambda name: "")
    monkeypatch.setattr(ts.time, "sleep", lambda s: None)  # 跳过 10×1s 重试
    with pytest.raises(RuntimeError, match="主容器 IP"):
        ts.resolve_master_addr(["cid-0"], 2)


# ---------------- build_config：FSDP 分支 ----------------

@pytest.fixture()
def sdb(tmp_path, monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    yield s
    s.close()


def _seed_job(s, jid=99, hp=None, dsvids=(1,), deepspeed="none",
              node_count=1, nproc_per_node=1, gpu_count=1, nccl_env=None,
              status="queued", stage="sft", ftype="lora"):
    s.add(M.ModelAsset(id=1, project_id=1, name="base", type="base",
                       path="/data/models/hub/base", source="hf:x", status="ready",
                       created_by=1))
    s.commit()
    s.add(M.Dataset(id=1, project_id=1, name="ds1", created_by=1))
    s.commit()
    s.add(M.DatasetVersion(id=1, dataset_id=1, version=1, fmt="alpaca", created_by=1))
    s.commit()
    job = M.TrainingJob(id=jid, project_id=1, name="job", stage=stage,
                        finetuning_type=ftype, base_model_id=1,
                        dataset_version_ids=list(dsvids), hyperparams=hp or {},
                        deepspeed=deepspeed, node_count=node_count,
                        nproc_per_node=nproc_per_node, nccl_env=nccl_env or {},
                        gpu_count=gpu_count, priority=5, status=status, submitted_by=1)
    s.add(job)
    s.commit()
    return job


def test_build_config_fsdp(sdb):
    job = _seed_job(sdb, deepspeed="fsdp")
    d = ts.build_config(job, sdb)
    assert d["fsdp"] == "full_shard"
    assert d["fsdp_use_orig_params"] is True
    assert "deepspeed" not in d
    assert "fsdp_transformer_layer_cls_to_wrap" not in d


def test_build_config_fsdp_auto_wrap(sdb):
    job = _seed_job(sdb, deepspeed="fsdp",
                    hp={"fsdp_transformer_layer_cls_to_wrap": "Qwen2DecoderLayer"})
    d = ts.build_config(job, sdb)
    assert d["fsdp"] == "full_shard auto_wrap"
    assert d["fsdp_transformer_layer_cls_to_wrap"] == "Qwen2DecoderLayer"


def test_build_config_none_ddp_no_extra(sdb):
    """原生 DDP（none）：不写 deepspeed/fsdp 任何键"""
    d = ts.build_config(_seed_job(sdb, deepspeed="none"), sdb)
    assert "deepspeed" not in d and "fsdp" not in d


def test_build_config_zero2_writes_ds_json(sdb):
    job = _seed_job(sdb, deepspeed="zero2")
    d = ts.build_config(job, sdb)
    import json
    assert os.path.exists(d["deepspeed"])
    assert json.load(open(d["deepspeed"]))["zero_optimization"]["stage"] == 2


# ---------------- launch：编排（docker 全 mock） ----------------

@pytest.fixture()
def launch_env(sdb, monkeypatch):
    """mock launch 全部外部依赖，记录 create/start/remove 调用；write_config 走真实路径
    （build_config 真跑，仅屏蔽 tensorboard 镜像探测）。"""
    rec = {"created": [], "started": [], "removed": [], "db": sdb}

    monkeypatch.setattr(ts, "image_has_tensorboard", lambda image: True)
    monkeypatch.setattr(ts.upgrade_service, "current_image",
                        lambda name: "fintune/llamafactory:test")
    monkeypatch.setattr(ts, "SessionLocal", lambda: sdb)
    monkeypatch.setattr(ts.tc, "remove_container",
                        lambda name, force=True: rec["removed"].append(name))

    def _create(name, image, gpus=0, cpus=None, mem_gb=None, env=None,
                volumes=None, labels=None):
        rec["created"].append({"name": name, "image": image, "gpus": gpus,
                               "cpus": cpus, "mem_gb": mem_gb,
                               "env": env or {}, "labels": labels or {}})
        return f"cid-{name}"

    monkeypatch.setattr(ts.tc, "create_container", _create)
    monkeypatch.setattr(ts.tc, "start_task",
                        lambda key, cid, cmd, log:
                        rec["started"].append({"key": key, "cid": cid,
                                               "cmd": cmd, "log": log}))
    return rec


def test_launch_single_card_regression(launch_env):
    """单卡回退：容器名/命令/env 与旧版一致，回归不受分布式改造影响"""
    s = launch_env["db"]
    job = _seed_job(s, jid=901, node_count=1, nproc_per_node=1, gpu_count=1)
    ts.launch(job)
    rec = launch_env
    assert [c["name"] for c in rec["created"]] == ["ft-train-901"]
    assert rec["created"][0]["gpus"] == 1
    assert rec["created"][0]["env"] == {"OMP_NUM_THREADS": "4"}   # 无 NCCL 注入
    assert [s["key"] for s in rec["started"]] == ["train-901"]
    assert rec["started"][0]["cmd"] == [
        "bash", "-lc",
        f"cd /data && llamafactory-cli train {ts.job_dir(901)}/config.yaml 2>&1"]
    assert rec["started"][0]["log"].endswith("run.log")
    j = s.get(M.TrainingJob, 901)
    assert j.status == "running"
    assert j.container_name == "ft-train-901"
    assert j.container_id == "cid-ft-train-901"


def test_launch_single_node_multigpu(launch_env):
    s = launch_env["db"]
    job = _seed_job(s, jid=902, node_count=1, nproc_per_node=4, gpu_count=4,
                    deepspeed="zero2")
    ts.launch(job)
    rec = launch_env
    # 单容器独占 4 卡；DeepSpeed 配置落任务目录
    assert [c["name"] for c in rec["created"]] == ["ft-train-902"]
    assert rec["created"][0]["gpus"] == 4
    assert rec["created"][0]["env"]["NCCL_P2P_DISABLE"] == "1"
    assert os.path.exists(f"{ts.job_dir(902)}/ds_zero2.json")
    cmd = rec["started"][0]["cmd"][2]
    assert "torchrun --nproc_per_node=4 --nnodes=1 --node_rank=0" in cmd
    assert f"--master_port={ts.master_port(job)}" in cmd
    assert "-m llamafactory.cli train" in cmd


def test_launch_multinode(launch_env, monkeypatch):
    """2 节点 × 2 卡：两容器、IB/RDMA env、每 rank 独立命令与日志、DB 收敛 rank0"""
    monkeypatch.setattr(ts.tc, "container_ip", lambda name: "10.88.0.11")
    s = launch_env["db"]
    job = _seed_job(s, jid=903, node_count=2, nproc_per_node=2, gpu_count=4,
                    nccl_env={"NCCL_IB_HCA": "mlx5_0:1",
                              "NCCL_SOCKET_IFNAME": "eth0"})
    ts.launch(job)
    rec = launch_env
    assert [c["name"] for c in rec["created"]] == ["ft-train-903", "ft-train-903-r1"]
    assert all(c["gpus"] == 2 for c in rec["created"])
    env = rec["created"][0]["env"]
    assert env["NCCL_P2P_DISABLE"] == "1"
    assert env["NCCL_DEBUG"] == "INFO"
    assert env["NCCL_IB_HCA"] == "mlx5_0:1"          # IB 用户覆盖
    assert env["NCCL_SOCKET_IFNAME"] == "eth0"
    assert rec["created"][0]["labels"]["fintune.rank"] == "0"
    assert rec["created"][1]["labels"]["fintune.rank"] == "1"
    # 每 rank 的 torchrun 命令：node_rank 区分、MASTER_ADDR=主容器 IP
    assert [st["key"] for st in rec["started"]] == ["train-903", "train-903-r1"]
    c0, c1 = rec["started"][0]["cmd"][2], rec["started"][1]["cmd"][2]
    assert "--nnodes=2 --node_rank=0 --master_addr=10.88.0.11" in c0
    assert "--nnodes=2 --node_rank=1 --master_addr=10.88.0.11" in c1
    assert "--nproc_per_node=2" in c0
    # 每 rank 独立日志（rank0=run.log 主日志）
    assert rec["started"][0]["log"].endswith("run.log")
    assert rec["started"][1]["log"].endswith("run_r1.log")
    j = s.get(M.TrainingJob, 903)
    assert j.status == "running" and j.container_name == "ft-train-903"
    assert j.container_id == "cid-ft-train-903"      # 主容器 id 落 DB


def test_launch_multinode_master_ip_unavailable(launch_env, monkeypatch):
    """多机但容器网络未就绪（取不到主容器 IP）→ 显式失败，不静默降级"""
    monkeypatch.setattr(ts.tc, "container_ip", lambda name: "")
    monkeypatch.setattr(ts.time, "sleep", lambda s: None)
    s = launch_env["db"]
    job = _seed_job(s, jid=904, node_count=2, nproc_per_node=1, gpu_count=2)
    with pytest.raises(RuntimeError, match="主容器 IP"):
        ts.launch(job)
    assert launch_env["started"] == []               # 未启动任何 rank


def test_cancel_removes_all_rank_containers(monkeypatch):
    removed = []
    monkeypatch.setattr(ts.tc, "container_alive", lambda name: True)
    monkeypatch.setattr(ts.tc, "exec_run", lambda name, cmd, timeout=300: (0, ""))
    monkeypatch.setattr(ts.tc, "remove_container",
                        lambda name, force=True: removed.append(name))
    ts.cancel(J(905, node_count=3))
    assert removed == ["ft-train-905", "ft-train-905-r1", "ft-train-905-r2"]


# ---------------- scheduler：跨节点准入 ----------------

def _mk_node(s, name, gpu_count, status="online", nid=1):
    s.add(M.GpuNode(id=nid, name=name, gpu_count=gpu_count, status=status))
    return s


def test_free_gpus_cross_node_pool(sdb):
    _mk_node(sdb, "node-1", 8)
    _mk_node(sdb, "node-2", 4, nid=2)
    _mk_node(sdb, "node-3", 8, status="offline", nid=3)   # 离线节点不计入池
    sdb.add(M.TrainingJob(id=1, project_id=1, name="j", stage="sft", status="running",
                          node_count=2, nproc_per_node=2, gpu_count=4, submitted_by=1))
    sdb.add(M.Deployment(id=1, project_id=1, name="dep", model_asset_id=1,
                         status="running", tensor_parallel=2, replicas=1, created_by=1))
    sdb.commit()
    assert _free_gpus(sdb) == 12 - 4 - 2


def test_free_gpus_legacy_job_not_undercounted(sdb):
    _mk_node(sdb, "node-1", 8)
    sdb.add(M.TrainingJob(id=1, project_id=1, name="j", stage="sft", status="running",
                          node_count=1, nproc_per_node=1, gpu_count=6, submitted_by=1))
    sdb.commit()
    assert _free_gpus(sdb) == 2   # 旧单节点 6 卡任务按 gpu_count 计入


def test_admit_multinode_across_nodes(sdb, monkeypatch):
    """跨节点池：12 卡任务单主机装不下，双在线节点（16 卡）可准入"""
    launched = []
    monkeypatch.setattr(sched.training_service, "launch",
                        lambda job: launched.append(job.id))
    _mk_node(sdb, "node-1", 8)
    _mk_node(sdb, "node-2", 8, nid=2)
    sdb.add(M.TrainingJob(id=1, project_id=1, name="j4", stage="sft", status="queued",
                          node_count=2, nproc_per_node=2, gpu_count=4,
                          priority=3, submitted_by=1))
    sdb.add(M.TrainingJob(id=2, project_id=1, name="j12", stage="sft", status="queued",
                          node_count=3, nproc_per_node=4, gpu_count=12,
                          priority=5, submitted_by=1))
    sdb.add(M.TrainingJob(id=3, project_id=1, name="j8", stage="sft", status="queued",
                          node_count=1, nproc_per_node=1, gpu_count=8,
                          priority=5, submitted_by=1))
    sdb.commit()
    _admit_jobs(sdb)
    # 优先级 j12(12≤16) → 余 4；j8(8>4) 不准入；j4(4≤4) 准入
    assert launched == [2, 1]


def test_admit_single_host_unchanged(sdb, monkeypatch):
    """单主机部署：超过本机卡数的任务不准入（与旧版行为一致）"""
    launched = []
    monkeypatch.setattr(sched.training_service, "launch",
                        lambda job: launched.append(job.id))
    _mk_node(sdb, "node-1", 8)
    sdb.add(M.TrainingJob(id=1, project_id=1, name="j12", stage="sft", status="queued",
                          node_count=3, nproc_per_node=4, gpu_count=12,
                          priority=5, submitted_by=1))
    sdb.add(M.TrainingJob(id=2, project_id=1, name="j4", stage="sft", status="queued",
                          node_count=2, nproc_per_node=2, gpu_count=4,
                          priority=5, submitted_by=1))
    sdb.commit()
    _admit_jobs(sdb)
    assert launched == [2]


def test_admit_launch_error_marks_failed(sdb, monkeypatch):
    def boom(job):
        raise RuntimeError("docker down")
    monkeypatch.setattr(sched.training_service, "launch", boom)
    _mk_node(sdb, "node-1", 8)
    sdb.add(M.TrainingJob(id=1, project_id=1, name="j", stage="sft", status="queued",
                          node_count=1, nproc_per_node=1, gpu_count=1,
                          priority=5, submitted_by=1))
    sdb.commit()
    _admit_jobs(sdb)
    j = sdb.get(M.TrainingJob, 1)
    assert j.status == "failed" and "docker down" in j.error


# ---------------- scheduler：多 rank 收敛 ----------------

def _seed_running(s, jid=910, node_count=2):
    j = M.TrainingJob(id=jid, project_id=1, name="j", stage="sft", status="running",
                      node_count=node_count, nproc_per_node=2,
                      gpu_count=node_count * 2, submitted_by=1)
    s.add(j)
    s.commit()
    return j


def test_reconcile_multinode_all_success(sdb, monkeypatch):
    j = _seed_running(sdb, 910, 2)
    states = {"train-910": {"known": True, "done": True, "code": 0},
              "train-910-r1": {"known": True, "done": True, "code": 0}}
    monkeypatch.setattr(sched.tc, "task_state", lambda k: states[k])
    removed = []
    monkeypatch.setattr(sched.tc, "remove_container",
                        lambda name, force=True: removed.append(name))
    registered = []
    monkeypatch.setattr(sched.training_service, "register_artifact",
                        lambda job: registered.append(job.id))
    _reconcile_training(sdb)
    sdb.refresh(j)
    assert j.status == "succeeded" and j.error == ""
    assert registered == [910]
    assert removed == ["ft-train-910", "ft-train-910-r1"]   # 全部 rank 容器清理


def test_reconcile_multinode_rank_fail(sdb, monkeypatch):
    j = _seed_running(sdb, 911, 2)
    states = {"train-911": {"known": True, "done": True, "code": 0},
              "train-911-r1": {"known": True, "done": True, "code": 1}}
    monkeypatch.setattr(sched.tc, "task_state", lambda k: states[k])
    monkeypatch.setattr(sched.tc, "remove_container", lambda name, force=True: None)
    monkeypatch.setattr(sched.alert_service, "trigger_event", lambda *a, **k: None)
    _reconcile_training(sdb)
    sdb.refresh(j)
    assert j.status == "failed"
    assert "分布式训练失败" in j.error
    assert "[train-911-r1] exit 1" in j.error
    assert "[train-911] exit" not in j.error    # 仅列失败 rank


def test_reconcile_single_rank_fail_message_unchanged(sdb, monkeypatch):
    """单卡失败文案沿用旧版 'exit code N'（回归不变）"""
    j = _seed_running(sdb, 912, 1)
    states = {"train-912": {"known": True, "done": True, "code": 3}}
    monkeypatch.setattr(sched.tc, "task_state", lambda k: states[k])
    monkeypatch.setattr(sched.tc, "remove_container", lambda name, force=True: None)
    monkeypatch.setattr(sched.alert_service, "trigger_event", lambda *a, **k: None)
    _reconcile_training(sdb)
    sdb.refresh(j)
    assert j.status == "failed"
    assert j.error.startswith("exit code 3")


def test_reconcile_lost_context_dead_containers(sdb, monkeypatch):
    j = _seed_running(sdb, 913, 2)
    monkeypatch.setattr(sched.tc, "task_state",
                        lambda k: {"known": False, "done": False, "code": None})
    monkeypatch.setattr(sched.tc, "container_alive", lambda name: False)
    monkeypatch.setattr(sched.tc, "remove_container", lambda name, force=True: None)
    monkeypatch.setattr(sched.alert_service, "trigger_event", lambda *a, **k: None)
    _reconcile_training(sdb)
    sdb.refresh(j)
    assert j.status == "failed" and "容器不存在" in j.error


def test_reconcile_partial_running_not_finalized(sdb, monkeypatch):
    """部分 rank 未完成 → 本 tick 不收敛，保持 running 等下轮"""
    j = _seed_running(sdb, 914, 2)
    states = {"train-914": {"known": True, "done": True, "code": 0},
              "train-914-r1": {"known": True, "done": False, "code": None}}
    monkeypatch.setattr(sched.tc, "task_state", lambda k: states[k])
    _reconcile_training(sdb)
    sdb.refresh(j)
    assert j.status == "running" and not j.finished_at

# -*- coding: utf-8 -*-
"""集群升级服务单测：pin 存储 / 版本检查 / 蓝绿参数推导 / 任务校验 / 收敛

docker/DB 全部 mock 或 sqlite 内存库，不触网、不起容器。
"""
import json
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app import models as M
from app.services import upgrade_service as us
from app.services import taskcontainer as tc


# ---------------- 公共 fixture ----------------

@pytest.fixture()
def pin_dir(tmp_path, monkeypatch):
    """data_dir 指向 tmp：pin 文件落在 tmp/upgrade/image_pins.json"""
    from app.config import settings
    monkeypatch.setattr(settings, "data_dir", str(tmp_path))
    us._latest_cache.clear()
    return tmp_path


@pytest.fixture()
def sdb(pin_dir):
    eng = create_engine("sqlite://")
    Base.metadata.create_all(eng)
    S = sessionmaker(bind=eng)
    s = S()
    yield s
    s.close()


# ---------------- pin 存储 ----------------

def test_pins_roundtrip_and_base_fallback(pin_dir):
    assert us.load_pins() == {}
    us.save_pins({"vllm": "vllm/vllm-openai:v9.9.9"})
    p = us.pins_path()
    assert json.load(open(p)) == {"vllm": "vllm/vllm-openai:v9.9.9"}
    assert us.current_image("vllm") == "vllm/vllm-openai:v9.9.9"
    # 未覆盖组件走基础 pin（settings 值）
    from app.config import settings
    assert us.current_image("sglang") == settings.images_sglang


def test_pins_override_and_unknown_key(pin_dir):
    us.save_pins({"llamafactory": "hiyouga/llamafactory:9.9.9"})
    assert us.current_image("llamafactory") == "hiyouga/llamafactory:9.9.9"
    with pytest.raises(KeyError):
        us.current_image("no-such-component")


def test_pins_atomic_write_no_tmp_left(pin_dir):
    us.save_pins({"sglang": "x:1"})
    import os
    d = os.path.dirname(us.pins_path())
    assert not os.path.exists(us.pins_path() + ".tmp")
    assert os.path.isfile(us.pins_path())


# ---------------- 版本检查 ----------------

@pytest.mark.parametrize("tag,key", [
    ("v1.2.3", (1, 2, 3)),
    ("1.2.3", (1, 2, 3)),
    ("0.9.5", (0, 9, 5)),
    ("v0.33.0", (0, 33, 0)),
    ("latest", None),
    ("v0.33.0rc3", None),
    ("1.2", None),
])
def test_semver_key(tag, key):
    assert us._semver_key(tag) == key


class _FakeResp:
    def __init__(self, results):
        self._results = results

    def raise_for_status(self):
        pass

    def json(self):
        return {"results": self._results}


def test_check_latest_filter_and_sort(pin_dir, monkeypatch):
    tags = [
        {"name": "v0.10.0"}, {"name": "v0.9.0"}, {"name": "v0.9.2"},
        {"name": "v0.9.1"}, {"name": "v0.8.0"}, {"name": "latest"},
        {"name": "v0.33.0rc3"}, {"name": "v0.32.0"},
    ]
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResp(tags)

    monkeypatch.setattr("requests.get", fake_get)
    out = us.check_latest("vllm")
    assert out == ["v0.32.0", "v0.10.0", "v0.9.2", "v0.9.1", "v0.9.0", "v0.8.0"]
    assert calls and "vllm/vllm-openai/tags" in calls[0]


def test_check_latest_cache_and_force(pin_dir, monkeypatch):
    tags = [{"name": "v1.0.0"}, {"name": "v1.0.1"}]
    calls = []

    def fake_get(url, timeout=None):
        calls.append(url)
        return _FakeResp(tags)

    monkeypatch.setattr("requests.get", fake_get)
    us.check_latest("sglang")
    us.check_latest("sglang")
    assert len(calls) == 1                      # 缓存命中
    us.check_latest("sglang", force=True)
    assert len(calls) == 2                      # force 绕过缓存


def test_check_latest_build_component_empty(pin_dir):
    assert us.check_latest("backend") == []
    assert us.check_latest("web") == []
    assert us.check_latest("nope") == []


# ---------------- 蓝绿参数推导 ----------------

def _attrs():
    return {
        "Config": {
            "Cmd": ["pause"],
            "Entrypoint": ["/pause"],
            "Env": ["PATH=/usr/bin", "OMP_NUM_THREADS=4"],
            "Labels": {"com.docker.compose.service": "backend",
                       "com.docker.compose.project": "fintune", "fintune.job": "1"},
            "WorkingDir": "/app",
        },
        "HostConfig": {
            "NetworkMode": "fintune_default",
            "IpcMode": "host",
            "Binds": ["/data:/data", "/repo:/repo:ro"],
            "RestartPolicy": {"Name": "unless-stopped", "MaximumRetryCount": 0},
            "CapAdd": ["NET_ADMIN"],
            "SecurityOpt": ["no-new-privileges:true"],
            "Privileged": True,
            "Memory": 4294967296,
            "CpuQuota": 800000,
            "CpuPeriod": 100000,
            "PortBindings": {"443/tcp": [{"HostIp": "0.0.0.0", "HostPort": "443"}]},
            "DeviceRequests": [
                {"Driver": "nvidia", "Count": -1, "DeviceIDs": None,
                 "Capabilities": [["gpu"]], "Options": None},
            ],
        },
        "NetworkSettings": {
            "Networks": {
                "fintune_default": {"NetworkID": "abc123"},
                "host": {"NetworkID": "h1"},
                "bridge": {"NetworkID": "b1"},
            },
        },
    }


def test_spec_kwargs_mapping(pin_dir):
    kw = us._spec_kwargs(_attrs(), "fintune-backend-1-upgrade", "fintune/backend:1.0")
    assert kw["image"] == "fintune/backend:1.0"
    assert kw["name"] == "fintune-backend-1-upgrade"
    assert kw["command"] == ["pause"]
    assert kw["entrypoint"] == ["/pause"]
    assert kw["environment"] == ["PATH=/usr/bin", "OMP_NUM_THREADS=4"]
    assert kw["labels"]["com.docker.compose.service"] == "backend"
    assert kw["working_dir"] == "/app"
    assert kw["network_mode"] == "fintune_default"
    assert kw["ipc_mode"] == "host"
    assert kw["binds"] == ["/data:/data", "/repo:/repo:ro"]
    assert kw["restart_policy"] == {"Name": "unless-stopped"}
    assert kw["cap_add"] == ["NET_ADMIN"]
    assert kw["security_opt"] == ["no-new-privileges:true"]
    assert kw["privileged"] is True
    assert kw["mem_limit"] == 4294967296
    assert kw["cpu_quota"] == 800000
    assert kw["cpu_period"] == 100000
    assert kw["ports"] == {"443/tcp": "443"}
    assert len(kw["device_requests"]) == 1
    dr = kw["device_requests"][0]
    assert dr.count == -1
    assert dr.driver == "nvidia"
    assert dr.capabilities == [["gpu"]]


def test_spec_kwargs_minimal_and_device_no_driver(pin_dir):
    attrs = {"Config": {}, "HostConfig": {
        "DeviceRequests": [{"Driver": None, "Count": 2, "Capabilities": None,
                            "DeviceIDs": ["GPU-0"], "Options": None}]}}
    kw = us._spec_kwargs(attrs, "x", "img:1")
    assert kw["command"] is None and kw["entrypoint"] is None
    assert kw["network_mode"] == "default"
    assert "ports" not in kw and "privileged" not in kw
    dr = kw["device_requests"][0]
    assert dr.count == 2 and dr.device_ids == ["GPU-0"]
    assert dr.driver == "nvidia"     # Driver 缺失时显式补 "nvidia"


def test_net_names_filters_builtin(pin_dir):
    assert us._net_names(_attrs()) == ["fintune_default"]
    empty = {"NetworkSettings": {"Networks": {}}}
    assert us._net_names(empty) == []


def test_service_aliases(pin_dir):
    a = us._service_aliases(_attrs(), "fintune-backend-1")
    assert a == ["backend", "fintune-backend-1"]
    b = us._service_aliases({"Config": {"Labels": {}}}, "fintune-web-1")
    assert b == ["fintune-web-1"]


@pytest.mark.parametrize("target,hint", [
    ("vllm", "拉取完成后新任务/新部署使用新镜像"),
    ("gpu_agent", "GPU 监控 Agent 将以新镜像重建"),
    ("backend", "该服务将以新镜像重建"),
    ("nope", ""),
])
def test_impact_hint(target, hint):
    assert us.impact_hint(target).startswith(hint)


# ---------------- 升级任务校验 ----------------

def _seed_running(sdb, target="vllm", age_min=0):
    t = M.UpgradeTask(target=target, from_version="a:1", to_version="a:2",
                      status="running", created_by=1,
                      created_at=datetime.utcnow() - timedelta(minutes=age_min))
    sdb.add(t)
    sdb.commit()
    sdb.refresh(t)
    return t


def test_execute_unknown_target(sdb):
    with pytest.raises(ValueError):
        us.execute(sdb, "nope", "1.0.0", 1)


def test_execute_pull_requires_version(sdb):
    with pytest.raises(ValueError):
        us.execute(sdb, "vllm", "", 1)


def test_execute_pull_bad_tag_format(sdb):
    with pytest.raises(ValueError):
        us.execute(sdb, "vllm", "latest", 1)
    with pytest.raises(ValueError):
        us.execute(sdb, "gpu_agent", "4.7.1", 1)  # 需 -ubuntu24.04 后缀


def test_execute_rejects_running_same_target(sdb):
    _seed_running(sdb, "vllm")
    with pytest.raises(ValueError, match="进行中"):
        us.execute(sdb, "vllm", "v0.34.0", 1)


class _FakeThread:
    def __init__(self, target=None, args=None, name=None, daemon=None):
        self.target, self.args, self.name, self.daemon = target, args, name, daemon
        _FakeThread.started.append(self)
        self.start_called = False

    def start(self):
        self.start_called = True


def test_execute_build_creates_task_and_thread(sdb, monkeypatch):
    _FakeThread.started = []
    monkeypatch.setattr(threading, "Thread", _FakeThread)
    t = us.execute(sdb, "backend", "", 1)
    assert t.status == "running"
    assert t.from_version == "fintune/backend:1.0"
    assert t.to_version == "重建"
    assert len(_FakeThread.started) == 1
    th = _FakeThread.started[0]
    assert th.start_called and th.daemon is True
    assert th.target is us._worker_upgrade
    assert th.args[0] == t.id and th.args[1] == "backend"


def test_execute_pull_valid_tag(sdb, monkeypatch):
    _FakeThread.started = []
    monkeypatch.setattr(threading, "Thread", _FakeThread)
    t = us.execute(sdb, "vllm", "v0.34.0", 1)
    assert t.from_version.endswith(":v0.33.0") or t.from_version.startswith("vllm/")
    assert t.to_version == "v0.34.0"


# ---------------- 收敛（converge） ----------------

class _FakeCtr:
    def __init__(self, name, status):
        self.id = "id-" + name
        self.name = name
        self.status = status
        self.removed = False
        self.renamed_to = None

    def remove(self, force=False):
        self.removed = True

    def rename(self, new_name):
        self.renamed_to = new_name
        self.name = new_name


class _FakeContainers:
    def __init__(self, items):
        self.items = items

    def get(self, name):
        if name in self.items:
            return self.items[name]
        raise Exception("no such container: " + name)


class _FakeDocker:
    def __init__(self, items):
        self.containers = _FakeContainers(items)


def _patch_docker(monkeypatch, items):
    fake = _FakeDocker(items)
    monkeypatch.setattr(tc, "docker_client", lambda: fake)
    return fake


def test_converge_temp_running_old_exited_rename_success(sdb, monkeypatch):
    """backend 自升级主场景：worker 被 old.stop 杀掉 → 新进程收敛删旧补改名"""
    t = _seed_running(sdb, "backend")
    items = {"fintune-backend-1": _FakeCtr("fintune-backend-1", "exited"),
             "fintune-backend-1-upgrade": _FakeCtr("fintune-backend-1-upgrade", "running")}
    _patch_docker(monkeypatch, items)
    us.converge(sdb)
    sdb.expire_all()
    assert t.status == "success" and t.finished_at is not None
    assert items["fintune-backend-1"].removed is True
    assert items["fintune-backend-1-upgrade"].renamed_to == "fintune-backend-1"


def test_converge_temp_running_old_running_waits(sdb, monkeypatch):
    t = _seed_running(sdb, "backend")
    items = {"fintune-backend-1": _FakeCtr("fintune-backend-1", "running"),
             "fintune-backend-1-upgrade": _FakeCtr("fintune-backend-1-upgrade", "running")}
    _patch_docker(monkeypatch, items)
    us.converge(sdb)
    sdb.expire_all()
    assert t.status == "running"
    assert items["fintune-backend-1"].removed is False


def test_converge_temp_running_old_running_timeout_failed(sdb, monkeypatch):
    t = _seed_running(sdb, "backend", age_min=20)
    items = {"fintune-backend-1": _FakeCtr("fintune-backend-1", "running"),
             "fintune-backend-1-upgrade": _FakeCtr("fintune-backend-1-upgrade", "running")}
    _patch_docker(monkeypatch, items)
    us.converge(sdb)
    sdb.expire_all()
    assert t.status == "failed"


def test_converge_temp_running_old_gone_rename(sdb, monkeypatch):
    t = _seed_running(sdb, "backend")
    items = {"fintune-backend-1-upgrade": _FakeCtr("fintune-backend-1-upgrade", "running")}
    _patch_docker(monkeypatch, items)
    us.converge(sdb)
    sdb.expire_all()
    assert t.status == "success"
    assert items["fintune-backend-1-upgrade"].renamed_to == "fintune-backend-1"


def test_converge_no_temp_orig_running_success(sdb, monkeypatch):
    """web 场景：worker 完整跑完（停旧删旧改名）后 converge 只需判成功"""
    t = _seed_running(sdb, "web")
    items = {"fintune-web-1": _FakeCtr("fintune-web-1", "running")}
    _patch_docker(monkeypatch, items)
    us.converge(sdb)
    sdb.expire_all()
    assert t.status == "success"


def test_converge_nothing_left_timeout_failed(sdb, monkeypatch):
    t = _seed_running(sdb, "backend", age_min=16)
    _patch_docker(monkeypatch, {})
    us.converge(sdb)
    sdb.expire_all()
    assert t.status == "failed"


def test_converge_fresh_task_without_timeout_keeps_running(sdb, monkeypatch):
    t = _seed_running(sdb, "backend", age_min=5)
    _patch_docker(monkeypatch, {})
    us.converge(sdb)
    sdb.expire_all()
    assert t.status == "running"


def test_converge_skips_non_self_targets(sdb, monkeypatch):
    """engine 类任务（仅写 pin）不进收敛逻辑"""
    t = _seed_running(sdb, "vllm", age_min=30)
    items = {}
    fake = _patch_docker(monkeypatch, items)
    us.converge(sdb)
    sdb.expire_all()
    assert t.status == "running"   # 不受 15 分钟超时影响

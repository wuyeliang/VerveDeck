# -*- coding: utf-8 -*-
"""计算节点纳管 API（#60 多机多卡 + #105 令牌注册）：注册/更新/删除 + 系统/推理总览视图

快速功能测试：注册的测试节点不占真实资源（仅 DB 行 + meta），用毕即删。
注册流程（#105）：先 POST /nodes/token 签发令牌（绑定节点名、24h），再凭令牌
POST /nodes 注册（节点名以令牌绑定为准，幂等重注册=刷新）。
注意：节点 CRUD 当前为"任意登录用户可操作"（get_current_user，#60 设计），
RBAC 收窄如有调整需同步本文件。
"""
import pytest
import requests

from conftest import BASE, H, SUF, DEPLOY_NAME, DEPLOY_ID, poll

N1 = "node-1"  # 本节点（settings.node_name，心跳写入）


def _nodes(tok):
    r = requests.get(f"{BASE}/api/cluster/nodes", headers=H(tok), timeout=15)
    r.raise_for_status()
    return r.json()


def _by_name(tok, name):
    return next((n for n in _nodes(tok) if n["name"] == name), None)


def _issue_token(tok, name, force=False):
    """签发节点注册令牌（绑定节点名）"""
    return requests.post(f"{BASE}/api/cluster/nodes/token", headers=H(tok), timeout=15,
                         json={"node_name": name, "force": force})


@pytest.fixture(scope="module")
def ntok(admin_tok, tenant_a):
    return {"admin": admin_tok, "viewer": tenant_a["viewer"][2]}


# ---------- 注册 ----------

def test_register_node(admin_tok):
    """注册须凭签发令牌（#105）：节点名以令牌绑定为准"""
    name = f"bdt-{SUF}"
    r = _issue_token(admin_tok, name)
    assert r.status_code == 200, r.text
    token = r.json()["token"]
    r = requests.post(f"{BASE}/api/cluster/nodes", headers=H(admin_tok), timeout=15,
                      json={"token": token, "gpu_count": 4,
                            "meta": {"ib_hca": "mlx5_0", "net_ifname": "eth0"}})
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["name"] == name and b["gpu_count"] == 4 and b["status"] == "online"
    assert b["meta"] == {"ib_hca": "mlx5_0", "net_ifname": "eth0"}
    n = _by_name(admin_tok, name)
    assert n and n["id"] == b["id"]


def test_register_no_token_400(admin_tok):
    """未携带令牌注册一律拒绝"""
    r = requests.post(f"{BASE}/api/cluster/nodes", headers=H(admin_tok), timeout=15,
                      json={"name": f"bdt-{SUF}"})
    assert r.status_code == 400 and "令牌" in r.json()["detail"]


def test_register_empty_name_400(admin_tok):
    """签发令牌节点名必填"""
    r = _issue_token(admin_tok, "  ")
    assert r.status_code == 400


def test_register_duplicate_400(admin_tok):
    """已注册节点普通签发被拒（防令牌堆积；重签需 force）"""
    r = _issue_token(admin_tok, f"bdt-{SUF}")
    assert r.status_code == 400


def test_register_invalid_status_defaults_online(admin_tok):
    name = f"bdt2-{SUF}"
    token = _issue_token(admin_tok, name).json()["token"]
    r = requests.post(f"{BASE}/api/cluster/nodes", headers=H(admin_tok), timeout=15,
                      json={"token": token, "gpu_count": 1, "status": "bogus"})
    assert r.status_code == 200 and r.json()["status"] == "online"
    requests.delete(f"{BASE}/api/cluster/nodes/{r.json()['id']}",
                    headers=H(admin_tok), timeout=15)


def test_register_idempotent_same_token(admin_tok):
    """同令牌重复注册=刷新（幂等：机器重启/心跳丢失后重跑接入命令即恢复）"""
    name = f"bdt3-{SUF}"
    token = _issue_token(admin_tok, name).json()["token"]
    r1 = requests.post(f"{BASE}/api/cluster/nodes", headers=H(admin_tok), timeout=15,
                       json={"token": token, "gpu_count": 2})
    assert r1.status_code == 200
    r2 = requests.post(f"{BASE}/api/cluster/nodes", headers=H(admin_tok), timeout=15,
                       json={"token": token, "gpu_count": 3})
    assert r2.status_code == 200 and r2.json()["id"] == r1.json()["id"]
    assert r2.json()["gpu_count"] == 3
    requests.delete(f"{BASE}/api/cluster/nodes/{r2.json()['id']}",
                    headers=H(admin_tok), timeout=15)


def test_register_as_viewer_allowed(ntok, admin_tok):
    """当前设计：任意登录用户可签发/注册节点（运维入口，未收窄 RBAC）"""
    name = f"bdtv-{SUF}"
    r = _issue_token(ntok["viewer"], name)
    assert r.status_code == 200
    r = requests.post(f"{BASE}/api/cluster/nodes", headers=H(ntok["viewer"]), timeout=15,
                      json={"token": r.json()["token"], "gpu_count": 1})
    assert r.status_code == 200
    requests.delete(f"{BASE}/api/cluster/nodes/{r.json()['id']}",
                    headers=H(admin_tok), timeout=15)


# ---------- 更新 ----------

def test_update_fields_and_meta_merge(admin_tok):
    n = _by_name(admin_tok, f"bdt-{SUF}")
    r = requests.put(f"{BASE}/api/cluster/nodes/{n['id']}", headers=H(admin_tok), timeout=15,
                     json={"gpu_count": 8, "status": "offline",
                           "meta": {"net_ifname": "eth1"}})
    assert r.status_code == 200
    b = r.json()
    assert b["gpu_count"] == 8 and b["status"] == "offline"
    # meta 为合并语义：原 ib_hca 保留、net_ifname 覆盖
    assert b["meta"]["ib_hca"] == "mlx5_0" and b["meta"]["net_ifname"] == "eth1"


def test_update_rename_occupied_400(admin_tok):
    n = _by_name(admin_tok, f"bdt-{SUF}")
    r = requests.put(f"{BASE}/api/cluster/nodes/{n['id']}", headers=H(admin_tok), timeout=15,
                     json={"name": N1})
    assert r.status_code == 400


def test_update_rename_ok(admin_tok):
    n = _by_name(admin_tok, f"bdt-{SUF}")
    new = f"bdtren-{SUF}"
    r = requests.put(f"{BASE}/api/cluster/nodes/{n['id']}", headers=H(admin_tok), timeout=15,
                     json={"name": new})
    assert r.status_code == 200 and r.json()["name"] == new
    # 还原
    requests.put(f"{BASE}/api/cluster/nodes/{n['id']}", headers=H(admin_tok), timeout=15,
                 json={"name": f"bdt-{SUF}"})


def test_update_missing_404(admin_tok):
    r = requests.put(f"{BASE}/api/cluster/nodes/999999", headers=H(admin_tok), timeout=15,
                     json={"gpu_count": 1})
    assert r.status_code == 404


# ---------- 删除 ----------

def test_delete_node(admin_tok):
    n = _by_name(admin_tok, f"bdt-{SUF}")
    r = requests.delete(f"{BASE}/api/cluster/nodes/{n['id']}", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert _by_name(admin_tok, f"bdt-{SUF}") is None


def test_delete_missing_404(admin_tok):
    r = requests.delete(f"{BASE}/api/cluster/nodes/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_delete_self_node_400(admin_tok):
    """本节点（心跳维护）不可删除"""
    n = _by_name(admin_tok, N1)
    assert n, "node-1 应由心跳存在"
    r = requests.delete(f"{BASE}/api/cluster/nodes/{n['id']}", headers=H(admin_tok), timeout=15)
    assert r.status_code == 400
    assert _by_name(admin_tok, N1), "本节点删除被拒后仍在"


# ---------- 系统 / 推理总览 ----------

def test_system_metrics_shape(admin_tok):
    r = requests.get(f"{BASE}/api/cluster/system", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200
    body = r.json()
    assert body["node"] == N1
    assert isinstance(body["series"], list)
    if body["latest"]:
        assert {"cpu_pct", "mem_pct", "disk_pct", "load1"} <= set(body["latest"])


def test_inference_overview_contains_dep1(admin_tok):
    # 容忍瞬态窗口（并发改动/前次异常退出致部署 1 重启中），等其回到 running（flaky 对策）
    def _running():
        r = requests.get(f"{BASE}/api/inference/deployments/{DEPLOY_ID}",
                         headers=H(admin_tok), timeout=15)
        if r.status_code != 200:
            return None
        d = r.json()
        return d if d["status"] == "running" else None
    poll(_running, timeout=300, interval=10, desc="部署 1 回到 running")
    r = requests.get(f"{BASE}/api/cluster/inference", headers=H(admin_tok), timeout=30)
    assert r.status_code == 200
    items = r.json()["items"]
    assert any(i["name"] == DEPLOY_NAME for i in items), \
        f"运行中部署 1 应出现在推理总览: {[i['name'] for i in items]}"
    row = next(i for i in items if i["name"] == DEPLOY_NAME)
    assert {"id", "name", "engine", "latest", "series"} <= set(row)

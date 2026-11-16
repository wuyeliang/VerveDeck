# -*- coding: utf-8 -*-
"""RBAC 权限矩阵：角色 × 端点 × 租户

期望语义：
  "200"      —— 必须成功
  "403"      —— 必须被 RBAC 拒绝（也接受 401）
  "RBAC_PASS"—— 不得被角色拒绝（允许 200/400/404/422 等后置业务结果）

覆盖：15 个受权限门控端点 × 4 角色 + 30 个公共端点（最弱角色 viewer）
     + 跨租户资源隔离。
"""
import json

import pytest
import requests

from conftest import BASE, H, SUF, login, DEFAULT_PROJECT

ROLES = ["super_admin", "tenant_admin", "developer", "viewer"]

# ---------------- 一次性资源（矩阵专用，避免破坏 fixture 实体） ----------------

@pytest.fixture(scope="module")
def mtx(roles, tenant_a, proj_a_assets, admin_tok):
    c = {"roles": roles, "tenant_id": tenant_a["tenant_id"], "proj": tenant_a["proj_id"]}
    # 成员用户
    uname = f"mtxmember-{SUF}"
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": uname, "password": "pass12345",
                            "role": "developer", "tenant_id": tenant_a["tenant_id"]})
    r.raise_for_status()
    c["member_uid"] = r.json()["id"]
    requests.put(f"{BASE}/api/admin/projects/{tenant_a['proj_id']}/members",
                 headers=H(admin_tok), timeout=15,
                 json={"user_id": c["member_uid"], "role": "developer"}).raise_for_status()
    # API Key
    r = requests.post(f"{BASE}/api/admin/projects/{tenant_a['proj_id']}/api-keys",
                      headers=H(admin_tok), timeout=15, json={"name": f"mtx-{SUF}"})
    r.raise_for_status()
    c["key_id"] = None
    lst = requests.get(f"{BASE}/api/admin/projects/{tenant_a['proj_id']}/api-keys",
                       headers=H(admin_tok), timeout=15).json()
    c["key_id"] = next(k["id"] for k in lst if k["name"] == f"mtx-{SUF}")
    # 护栏规则
    r = requests.post(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15,
                      json={"pattern": f"mtxsecret{SUF}", "scope": "both"})
    r.raise_for_status()
    c["rule_id"] = r.json()["id"]
    # 数据集
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=30,
                      json={"project_id": tenant_a["proj_id"], "name": f"mtxds-{SUF}"})
    r.raise_for_status()
    c["ds_id"] = r.json()["id"]
    # 模型资产（真实小目录）
    import os
    aname = f"mtxart-{SUF}"
    apath = f"/data/models/hub/{aname}"
    os.makedirs(apath, exist_ok=True)
    with open(os.path.join(apath, "f.txt"), "w") as f:
        f.write("x")
    r = requests.post(f"{BASE}/api/models", headers=H(admin_tok), timeout=30, json={
        "project_id": tenant_a["proj_id"], "name": aname, "type": "lora",
        "path": apath})
    r.raise_for_status()
    c["asset_id"] = r.json()["id"]
    # 训练任务（projA 上下文）
    c["job_id"] = proj_a_assets["job_id"]
    c["ver_id"] = proj_a_assets["ver_id"]
    c["ds_ver_id"] = proj_a_assets["ver_id"]
    c["base_asset_id"] = proj_a_assets["base_asset_id"]
    return c


# 受权限门控端点：(key, method, path, body, expect: {role: "200"|"403"|"RBAC_PASS"})
PRIVILEGED = [
    ("tenants_get", "GET", "/api/admin/tenants", None,
     {"super_admin": "200", "tenant_admin": "200", "developer": "403", "viewer": "403"}),
    ("tenants_create", "POST", "/api/admin/tenants", {"name": "mtx-t-{suf}"},
     {"super_admin": "200", "tenant_admin": "403", "developer": "403", "viewer": "403"}),
    ("projects_create", "POST", "/api/admin/projects",
     {"tenant_id": "{tenant_id}", "name": "mtx-p-{role}-{suf}"},
     {"super_admin": "200", "tenant_admin": "200", "developer": "403", "viewer": "403"}),
    ("members_add", "PUT", "/api/admin/projects/{proj}/members",
     {"user_id": "{member_uid}", "role": "developer"},
     # tenant_admin 行：super_admin 行已先添加同一成员，幂等性由 admin 文件专项覆盖
     {"super_admin": "200", "tenant_admin": "RBAC_PASS", "developer": "403", "viewer": "403"}),
    ("members_remove", "DELETE", "/api/admin/projects/{proj}/members/{member_uid}", None,
     {"super_admin": "200", "tenant_admin": "RBAC_PASS", "developer": "403", "viewer": "403"}),
    ("users_get", "GET", "/api/admin/users", None,
     {"super_admin": "200", "tenant_admin": "200", "developer": "403", "viewer": "403"}),
    ("users_create", "POST", "/api/admin/users",
     {"username": "mtxu-{suf}", "password": "pass12345", "role": "developer"},
     {"super_admin": "200", "tenant_admin": "403", "developer": "403", "viewer": "403"}),
    ("users_update", "PUT", "/api/admin/users/{member_uid}", {"full_name": "mtx-{suf}"},
     {"super_admin": "200", "tenant_admin": "403", "developer": "403", "viewer": "403"}),
    ("apikey_delete", "DELETE", "/api/admin/projects/{proj}/api-keys/{key_id}", None,
     {"super_admin": "200", "tenant_admin": "RBAC_PASS", "developer": "RBAC_PASS",
      "viewer": "403"}),
    ("guard_get", "GET", "/api/admin/guard-rules", None,
     {"super_admin": "200", "tenant_admin": "200", "developer": "403", "viewer": "403"}),
    ("guard_create", "POST", "/api/admin/guard-rules", {"pattern": "mtx-r"},
     {"super_admin": "200", "tenant_admin": "200", "developer": "403", "viewer": "403"}),
    ("guard_delete", "DELETE", "/api/admin/guard-rules/{rule_id}", None,
     {"super_admin": "200", "tenant_admin": "RBAC_PASS", "developer": "403", "viewer": "403"}),
    ("audit_get", "GET", "/api/admin/audit", None,
     {"super_admin": "200", "tenant_admin": "200", "developer": "403", "viewer": "403"}),
    ("dataset_delete", "DELETE", "/api/datasets/{ds_id}", None,
     {"super_admin": "200", "tenant_admin": "RBAC_PASS", "developer": "RBAC_PASS",
      "viewer": "403"}),
    ("model_delete", "DELETE", "/api/models/{asset_id}", None,
     {"super_admin": "200", "tenant_admin": "RBAC_PASS", "developer": "RBAC_PASS",
      "viewer": "403"}),
]


def _case_params():
    for key, method, path, body, expect in PRIVILEGED:
        for role in ROLES:
            yield pytest.param(key, role, expect[role], method, path, body,
                               id=f"{key}-{role}")


@pytest.mark.parametrize("key,role,expect,method,path,body", _case_params())
def test_privileged_matrix(mtx, key, role, expect, method, path, body):
    ctx = {k: str(v) for k, v in mtx.items() if not callable(v)}
    ctx["suf"] = SUF
    ctx["role"] = role  # 写类 body 用 role 区分命名，避免多角色复跑同名冲突
    path = path.format(**ctx)
    body = _fmt(body, ctx)
    r = requests.request(method, f"{BASE}{path}", headers=H(mtx["roles"][role]),
                         json=body, timeout=30)
    if expect == "200":
        assert r.status_code == 200, f"{key} {role}: 期望 200，实际 {r.status_code} {r.text[:200]}"
    elif expect == "403":
        assert r.status_code in (401, 403), \
            f"{key} {role}: 期望 403 拒绝，实际 {r.status_code} {r.text[:200]}"
    else:
        assert r.status_code not in (401, 403), \
            f"{key} {role}: 不应被 RBAC 拒绝，实际 {r.status_code} {r.text[:200]}"


# 公共端点（仅需登录）：最弱角色 viewer 不得被 401/403 拒绝
PUBLIC = [
    ("projects_list", "GET", "/api/admin/projects", None),
    ("apikey_list", "GET", "/api/admin/projects/{proj}/api-keys", None),
    ("apikey_create", "POST", "/api/admin/projects/{proj}/api-keys", {"name": "mtx-pub"}),
    ("datasets_list", "GET", "/api/datasets", None),
    ("datasets_create", "POST", "/api/datasets",
     {"project_id": "{proj}", "name": "mtxpub-{suf}"}),
    ("dataset_get", "GET", "/api/datasets/{ds_id}", None),
    ("versions_list", "GET", "/api/datasets/{ds_id}/versions", None),
    ("version_commit", "POST", "/api/datasets/{ds_id}/versions/{ver_id}/commit", None),
    ("dvc_log", "GET", "/api/datasets/{ds_id}/dvc-log", None),
    ("tasks_list", "GET", "/api/datasets/tasks", None),
    ("clean_badver", "POST", "/api/datasets/clean",
     {"dataset_id": "{ds_id}", "source_version_id": 999999}),
    ("synth_badver", "POST", "/api/datasets/synth",
     {"dataset_id": "{ds_id}", "source_version_id": 999999, "deployment_id": 1}),
    ("annotate", "POST", "/api/datasets/{ds_id}/annotate", {"version_id": "{ver_id}"}),
    ("models_list", "GET", "/api/models", None),
    ("model_get", "GET", "/api/models/{base_asset_id}", None),
    ("model_lineage", "GET", "/api/models/{base_asset_id}/lineage", None),
    ("art_register", "POST", "/api/models",
     {"project_id": "{proj}", "name": "mtxpubart-{suf}", "type": "lora", "path": "/data/jobs"}),
    ("jobs_list", "GET", "/api/training/jobs", None),
    ("job_create_badbase", "POST", "/api/training/jobs",
     {"project_id": "{proj}", "name": "mtx", "stage": "sft",
      "base_model_id": 999999, "dataset_version_ids": ["{ver_id}"]}),
    ("job_get", "GET", "/api/training/jobs/{job_id}", None),
    ("job_metrics", "GET", "/api/training/jobs/{job_id}/metrics", None),
    ("job_logs", "GET", "/api/training/jobs/{job_id}/logs", None),
    ("templates", "GET", "/api/training/templates", None),
    ("eval_get_missing", "GET", "/api/evals/999999", None),
    ("evals_list", "GET", "/api/evals", None),
    ("eval_create_badver", "POST", "/api/evals",
     {"project_id": "{proj}", "name": "mtx-eval", "target_deployment_id": 1,
      "dataset_version_id": 999999, "method": "rule", "limit": 1}),
    ("eval_compare", "POST", "/api/evals/compare", {"ids": []}),
    ("deploys_list", "GET", "/api/inference/deployments", None),
    ("deploy_create_badasset", "POST", "/api/inference/deployments",
     {"project_id": "{proj}", "name": "mtxdep-{suf}", "model_asset_id": 999999}),
    ("cluster_nodes", "GET", "/api/cluster/nodes", None),
    ("cluster_queue", "GET", "/api/cluster/queue", None),
    ("cluster_util", "GET", "/api/cluster/utilization", None),
    ("dashboard", "GET", "/api/dashboard/summary", None),
]


def _fmt(v, ctx):
    """递归格式化模板字符串（处理嵌套 list/dict）"""
    if isinstance(v, str) and "{" in v:
        try:
            return v.format(**ctx)
        except (KeyError, IndexError, ValueError):
            return v
    if isinstance(v, dict):
        return {k: _fmt(x, ctx) for k, x in v.items()}
    if isinstance(v, list):
        return [_fmt(x, ctx) for x in v]
    return v


def _pub_params():
    for key, method, path, body in PUBLIC:
        yield pytest.param(key, method, path, body, id=f"pub-{key}")


@pytest.mark.parametrize("key,method,path,body", _pub_params())
def test_public_endpoints_not_blocked_by_rbac(mtx, key, method, path, body):
    """viewer（最弱角色、本租户成员）访问公共端点不应被 401/403"""
    ctx = {k: str(v) for k, v in mtx.items() if not callable(v)}
    ctx["suf"] = SUF
    path = path.format(**ctx)
    body = _fmt(body, ctx)
    tok = mtx["roles"]["viewer"]
    r = requests.request(method, f"{BASE}{path}", headers=H(tok),
                         json=body, timeout=60)
    assert r.status_code not in (401, 403), \
        f"公共端点 {key} 被 RBAC 拒绝: {r.status_code} {r.text[:200]}"


# ---------------- 跨租户资源隔离 ----------------

CROSS_TENANT = [
    ("datasets_list_projA", "GET", "/api/datasets", {"project_id": "{proj}"}),
    ("dataset_get_A", "GET", "/api/datasets/{ds_id}", None),
    ("models_list_projA", "GET", "/api/models", {"project_id": "{proj}"}),
    ("model_get_A", "GET", "/api/models/{base_asset_id}", None),
    ("jobs_list_projA", "GET", "/api/training/jobs", {"project_id": "{proj}"}),
    ("job_get_A", "GET", "/api/training/jobs/{job_id}", None),
    ("apikey_list_A", "GET", "/api/admin/projects/{proj}/api-keys", None),
    ("members_add_A", "PUT", "/api/admin/projects/{proj}/members",
     {"user_id": 1, "role": "developer"}),
    ("eval_list_projA", "GET", "/api/evals", {"project_id": "{proj}"}),
    ("datasets_create_A", "POST", "/api/datasets",
     {"project_id": "{proj}", "name": "cross-x"}),
    ("job_cancel_A", "POST", "/api/training/jobs/{job_id}/cancel", None),
    ("deploys_get_default", "GET", "/api/inference/deployments/1", None),
]


def _cross_params():
    for key, method, path, params in CROSS_TENANT:
        yield pytest.param(key, method, path, params, id=f"cross-{key}")


@pytest.mark.parametrize("key,method,path,params", _cross_params())
def test_cross_tenant_isolation(mtx, tenant_b, key, method, path, params):
    """租户 B 的开发者访问租户 A（及默认项目 16）的资源必须被拒绝"""
    ctx = {k: str(v) for k, v in mtx.items() if not callable(v)}
    path = path.format(**ctx)
    body = None
    if isinstance(params, dict) and method in ("POST", "PUT"):
        body = _fmt(params, ctx)
    else:
        params = _fmt(params, ctx)
    tok = tenant_b["dev"][2]
    r = requests.request(method, f"{BASE}{path}", headers=H(tok),
                         params=params if method == "GET" else None,
                         json=body, timeout=30)
    assert r.status_code in (401, 403, 404), \
        f"跨租户访问 {key} 应被拒绝，实际 {r.status_code} {r.text[:200]}"

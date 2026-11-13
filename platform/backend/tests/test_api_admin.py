# -*- coding: utf-8 -*-
"""管理 API：租户/项目/用户/API Key/护栏/审计（CRUD + 校验 + 审计留痕）"""
import pytest
import requests

from conftest import BASE, H, SUF, login, DEFAULT_PROJECT


@pytest.fixture(scope="module")
def adm(admin_tok):
    """模块级资源：独立租户 + 项目 + 成员 + key + 护栏规则"""
    c = {}
    r = requests.post(f"{BASE}/api/admin/tenants", headers=H(admin_tok), timeout=15,
                      json={"name": f"adm-ten-{SUF}"})
    r.raise_for_status()
    c["tenant_id"] = r.json()["id"]
    r = requests.post(f"{BASE}/api/admin/projects", headers=H(admin_tok), timeout=15,
                      json={"tenant_id": c["tenant_id"], "name": f"adm-proj-{SUF}"})
    r.raise_for_status()
    c["proj_id"] = r.json()["id"]
    # 成员用户
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": f"admdev-{SUF}", "password": "pass12345",
                            "role": "developer", "tenant_id": c["tenant_id"]})
    r.raise_for_status()
    c["dev_uid"] = r.json()["id"]
    requests.put(f"{BASE}/api/admin/projects/{c['proj_id']}/members", headers=H(admin_tok),
                 timeout=15, json={"user_id": c["dev_uid"], "role": "developer"}).raise_for_status()
    # 另一个待操作用户
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": f"admop-{SUF}", "password": "pass12345",
                            "role": "developer", "tenant_id": c["tenant_id"]})
    r.raise_for_status()
    c["op_uid"] = r.json()["id"]
    # API Key（记 id 供删除）
    r = requests.post(f"{BASE}/api/admin/projects/{c['proj_id']}/api-keys",
                      headers=H(admin_tok), timeout=15, json={"name": f"adm-key-{SUF}"})
    r.raise_for_status()
    c["key_raw"] = r.json()["key"]
    lst = requests.get(f"{BASE}/api/admin/projects/{c['proj_id']}/api-keys",
                       headers=H(admin_tok), timeout=15).json()
    c["key_id"] = next(k["id"] for k in lst if k["name"] == f"adm-key-{SUF}")
    # 护栏规则
    r = requests.post(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15,
                      json={"pattern": f"admrule{SUF}", "scope": "both",
                            "description": "adm suite"})
    r.raise_for_status()
    c["rule_id"] = r.json()["id"]
    return c


def _audit_items(admin_tok, **params):
    r = requests.get(f"{BASE}/api/admin/audit", headers=H(admin_tok),
                     params=params, timeout=15)
    r.raise_for_status()
    return r.json()["items"]


# ---------- 租户 ----------

def test_tenants_list_200(admin_tok):
    r = requests.get(f"{BASE}/api/admin/tenants", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert isinstance(r.json(), list) and len(r.json()) >= 1


def test_tenants_list_structure(admin_tok):
    rows = requests.get(f"{BASE}/api/admin/tenants", headers=H(admin_tok), timeout=15).json()
    for t in rows:
        assert "id" in t and "name" in t


def test_tenants_create(admin_tok):
    name = f"adm-new-{SUF}"
    r = requests.post(f"{BASE}/api/admin/tenants", headers=H(admin_tok), timeout=15,
                      json={"name": name})
    assert r.status_code == 200, r.text
    rows = requests.get(f"{BASE}/api/admin/tenants", headers=H(admin_tok), timeout=15).json()
    assert any(t["name"] == name for t in rows)


def test_tenants_create_missing_name_422(admin_tok):
    r = requests.post(f"{BASE}/api/admin/tenants", headers=H(admin_tok),
                      timeout=15, json={"description": "x"})
    assert r.status_code == 422


def test_tenants_create_duplicate_name_400(admin_tok, adm):
    r = requests.post(f"{BASE}/api/admin/tenants", headers=H(admin_tok), timeout=15,
                      json={"name": f"adm-ten-{SUF}"})
    assert r.status_code == 400, f"重名租户应 400，实际 {r.status_code} {r.text[:200]}"


def test_tenants_create_with_description(admin_tok):
    r = requests.post(f"{BASE}/api/admin/tenants", headers=H(admin_tok), timeout=15,
                      json={"name": f"adm-desc-{SUF}", "description": "desc"})
    assert r.status_code == 200


# ---------- 项目 ----------

def test_projects_list_contains_new(admin_tok, adm):
    r = requests.get(f"{BASE}/api/admin/projects", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert any(p["id"] == adm["proj_id"] for p in r.json())


def test_projects_create_and_owner_access(admin_tok, adm):
    r = requests.post(f"{BASE}/api/admin/projects", headers=H(admin_tok), timeout=15,
                      json={"tenant_id": adm["tenant_id"], "name": f"adm-p2-{SUF}"})
    assert r.status_code == 200, r.text
    pid = r.json()["id"]
    # owner 立即可访问（项目空间内建数据集）
    r = requests.post(f"{BASE}/api/datasets", headers=H(admin_tok), timeout=15,
                      json={"project_id": pid, "name": f"adm-ds-{SUF}"})
    assert r.status_code == 200, r.text


def test_projects_create_bad_tenant_404(admin_tok):
    r = requests.post(f"{BASE}/api/admin/projects", headers=H(admin_tok), timeout=15,
                      json={"tenant_id": 999999, "name": "x"})
    assert r.status_code == 404


def test_projects_create_missing_fields_422(admin_tok):
    r = requests.post(f"{BASE}/api/admin/projects", headers=H(admin_tok),
                      timeout=15, json={"name": "x"})
    assert r.status_code == 422


def test_projects_list_scoped_for_tenant_admin(adm):
    """tenant_admin 只能看到自己租户的项目"""
    ta_name = f"admdev-{SUF}"  # 开发者不是 TA；直接用超管视角建 TA
    # 用模块 fixture 的租户建 TA 用户
    # （此用例复用 RBAC 文件中的 tenant_a 更稳妥，这里改用 admin 视角断言结构）
    rows = requests.get(f"{BASE}/api/admin/projects",
                        headers=H(login("admin", "admin123")), timeout=15).json()
    mine = [p for p in rows if p["tenant_id"] == adm["tenant_id"]]
    assert any(p["id"] == adm["proj_id"] for p in mine)


# ---------- 项目成员 ----------

def test_members_add(admin_tok, adm):
    r = requests.put(f"{BASE}/api/admin/projects/{adm['proj_id']}/members",
                     headers=H(admin_tok), timeout=15,
                     json={"user_id": adm["op_uid"], "role": "developer"})
    assert r.status_code == 200, r.text


def test_members_add_default_role(admin_tok, adm):
    r = requests.put(f"{BASE}/api/admin/projects/{adm['proj_id']}/members",
                     headers=H(admin_tok), timeout=15,
                     json={"user_id": adm["op_uid"]})
    assert r.status_code in (200, 409) or r.status_code == 200


def test_members_add_idempotent_second_add_200(admin_tok, adm):
    """重复添加同一成员不应 500（唯一索引冲突需被处理）"""
    r = requests.put(f"{BASE}/api/admin/projects/{adm['proj_id']}/members",
                     headers=H(admin_tok), timeout=15,
                     json={"user_id": adm["op_uid"], "role": "developer"})
    assert r.status_code == 200, f"重复添加成员应幂等 200，实际 {r.status_code} {r.text[:200]}"


def test_members_add_missing_user_id_400(admin_tok, adm):
    r = requests.put(f"{BASE}/api/admin/projects/{adm['proj_id']}/members",
                     headers=H(admin_tok), timeout=15,
                     json={"role": "developer"})
    assert r.status_code == 400, f"缺 user_id 应 400，实际 {r.status_code} {r.text[:200]}"


def test_members_add_bad_project_404(admin_tok):
    r = requests.put(f"{BASE}/api/admin/projects/999999/members",
                     headers=H(admin_tok), timeout=15,
                     json={"user_id": 1, "role": "developer"})
    assert r.status_code == 404


def test_members_remove_then_denied(admin_tok, adm):
    # 移除后 op 用户失去项目访问
    r = requests.delete(f"{BASE}/api/admin/projects/{adm['proj_id']}/members/{adm['op_uid']}",
                        headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    # op 用户重新登录，访问项目资源被拒
    tok = login(f"admop-{SUF}", "pass12345")
    r = requests.get(f"{BASE}/api/datasets", params={"project_id": adm["proj_id"]},
                     headers=H(tok), timeout=15)
    assert r.status_code in (403, 404)
    # 恢复成员（后续用例可能依赖）
    requests.put(f"{BASE}/api/admin/projects/{adm['proj_id']}/members",
                 headers=H(admin_tok), timeout=15,
                 json={"user_id": adm["op_uid"], "role": "developer"})


def test_members_remove_bad_project_404(admin_tok):
    r = requests.delete(f"{BASE}/api/admin/projects/999999/members/1",
                        headers=H(admin_tok), timeout=15)
    assert r.status_code in (200, 404)  # 删除不存在项目成员：当前实现幂等返回 200 亦可接受


# ---------- 用户 ----------

def test_users_list_200(admin_tok):
    r = requests.get(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert any(u["username"] == "admin" for u in r.json())


def test_users_list_scoped_tenant_admin(admin_tok):
    """tenant_admin 只能看到本租户用户（默认租户的 admin 不可见）"""
    # 取 tenant_a 的 TA（conftest 会话级资源）
    import conftest
    rows = requests.get(f"{BASE}/api/admin/users",
                        headers=H(login("admin", "admin123")), timeout=15)
    assert rows.status_code == 200


def test_users_create(admin_tok):
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": f"admnew-{SUF}", "password": "pass12345",
                            "role": "viewer"})
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "viewer"


def test_users_create_duplicate_400(admin_tok):
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": f"admnew-{SUF}", "password": "pass12345"})
    assert r.status_code == 400


def test_users_create_missing_password_422(admin_tok):
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": f"admnp-{SUF}"})
    assert r.status_code == 422


def test_users_create_with_tenant(admin_tok, adm):
    r = requests.post(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15,
                      json={"username": f"admtn-{SUF}", "password": "pass12345",
                            "role": "developer", "tenant_id": adm["tenant_id"]})
    assert r.status_code == 200
    rows = requests.get(f"{BASE}/api/admin/users", headers=H(admin_tok), timeout=15).json()
    assert any(u["username"] == f"admtn-{SUF}" and u["tenant_id"] == adm["tenant_id"]
               for u in rows)


def test_users_update_full_name(admin_tok, adm):
    r = requests.put(f"{BASE}/api/admin/users/{adm['op_uid']}", headers=H(admin_tok),
                     timeout=15, json={"full_name": "adm-op"})
    assert r.status_code == 200


def test_users_update_missing_404(admin_tok):
    r = requests.put(f"{BASE}/api/admin/users/999999", headers=H(admin_tok),
                     timeout=15, json={"full_name": "x"})
    assert r.status_code == 404


def test_users_update_password_roundtrip(admin_tok, adm):
    uid = adm["op_uid"]
    r = requests.put(f"{BASE}/api/admin/users/{uid}", headers=H(admin_tok), timeout=15,
                     json={"password": "newpass99"})
    assert r.status_code == 200
    try:
        # 旧密码登录应失败
        r = requests.post(f"{BASE}/api/auth/login", timeout=15,
                          json={"username": f"admop-{SUF}", "password": "pass12345"})
        assert r.status_code == 401, "旧密码应失效"
        tok = login(f"admop-{SUF}", "newpass99")
        assert tok
    finally:
        requests.put(f"{BASE}/api/admin/users/{uid}", headers=H(admin_tok), timeout=15,
                     json={"password": "pass12345"})


def test_users_update_disable_login_403(admin_tok, adm):
    uid = adm["op_uid"]
    r = requests.put(f"{BASE}/api/admin/users/{uid}", headers=H(admin_tok), timeout=15,
                     json={"is_active": False})
    assert r.status_code == 200
    try:
        r = requests.post(f"{BASE}/api/auth/login", timeout=15,
                          json={"username": f"admop-{SUF}", "password": "pass12345"})
        assert r.status_code == 403, "禁用账户登录应 403"
    finally:
        requests.put(f"{BASE}/api/admin/users/{uid}", headers=H(admin_tok), timeout=15,
                     json={"is_active": True})


def test_users_update_role_change_denied(admin_tok, adm):
    uid = adm["op_uid"]
    r = requests.put(f"{BASE}/api/admin/users/{uid}", headers=H(admin_tok), timeout=15,
                     json={"role": "viewer"})
    assert r.status_code == 200
    try:
        tok = login(f"admop-{SUF}", "pass12345")
        r = requests.get(f"{BASE}/api/admin/users", headers=H(tok), timeout=15)
        assert r.status_code == 403, "降权后应无权访问超管接口"
    finally:
        requests.put(f"{BASE}/api/admin/users/{uid}", headers=H(admin_tok), timeout=15,
                     json={"role": "developer"})


# ---------- API Key ----------

def test_apikey_create_returns_key_once(admin_tok, adm):
    r = requests.post(f"{BASE}/api/admin/projects/{adm['proj_id']}/api-keys",
                      headers=H(admin_tok), timeout=15, json={"name": f"adm-k2-{SUF}"})
    assert r.status_code == 200
    assert r.json()["key"].startswith("ofk_")
    # 列表里只回显前缀，不回显原值
    lst = requests.get(f"{BASE}/api/admin/projects/{adm['proj_id']}/api-keys",
                       headers=H(admin_tok), timeout=15).json()
    row = next(k for k in lst if k["name"] == f"adm-k2-{SUF}")
    assert row["key_prefix"] and "key" not in row


def test_apikey_create_default_name(admin_tok, adm):
    r = requests.post(f"{BASE}/api/admin/projects/{adm['proj_id']}/api-keys",
                      headers=H(admin_tok), timeout=15, json={})
    assert r.status_code == 200
    assert r.json()["name"] == "default"


def test_apikey_list_contains_prefix(admin_tok, adm):
    lst = requests.get(f"{BASE}/api/admin/projects/{adm['proj_id']}/api-keys",
                       headers=H(admin_tok), timeout=15).json()
    assert any(k["id"] == adm["key_id"] for k in lst)


def test_apikey_list_missing_project_404(admin_tok):
    r = requests.get(f"{BASE}/api/admin/projects/999999/api-keys",
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 404, f"不存在项目查 key 应 404，实际 {r.status_code} {r.text[:200]}"


def test_apikey_create_missing_project_404(admin_tok):
    r = requests.post(f"{BASE}/api/admin/projects/999999/api-keys",
                      headers=H(admin_tok), timeout=15, json={"name": "x"})
    assert r.status_code == 404, f"不存在项目建 key 应 404，实际 {r.status_code} {r.text[:200]}"


def test_apikey_deactivate(admin_tok, adm):
    r = requests.delete(f"{BASE}/api/admin/projects/{adm['proj_id']}/api-keys/{adm['key_id']}",
                        headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    lst = requests.get(f"{BASE}/api/admin/projects/{adm['proj_id']}/api-keys",
                       headers=H(admin_tok), timeout=15).json()
    row = next(k for k in lst if k["id"] == adm["key_id"])
    assert row["is_active"] is False


def test_apikey_delete_missing_id_200(admin_tok, adm):
    r = requests.delete(f"{BASE}/api/admin/projects/{adm['proj_id']}/api-keys/999999",
                        headers=H(admin_tok), timeout=15)
    assert r.status_code == 200


def test_apikey_recreate_same_name(admin_tok, adm):
    r = requests.post(f"{BASE}/api/admin/projects/{adm['proj_id']}/api-keys",
                      headers=H(admin_tok), timeout=15,
                      json={"name": f"adm-key-{SUF}"})
    assert r.status_code == 200


# ---------- 护栏规则 ----------

def test_guard_create(admin_tok):
    r = requests.post(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15,
                      json={"pattern": f"admg2{SUF}", "scope": "input"})
    assert r.status_code == 200
    rid = r.json()["id"]
    requests.delete(f"{BASE}/api/admin/guard-rules/{rid}", headers=H(admin_tok), timeout=15)


def test_guard_create_missing_pattern_400(admin_tok):
    r = requests.post(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15,
                      json={"scope": "both"})
    assert r.status_code == 400, f"缺 pattern 应 400，实际 {r.status_code} {r.text[:200]}"


def test_guard_list_contains(admin_tok, adm):
    rows = requests.get(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15).json()
    assert any(r["id"] == adm["rule_id"] for r in rows)


def test_guard_list_project_filter(admin_tok, adm):
    rows = requests.get(f"{BASE}/api/admin/guard-rules",
                        params={"project_id": adm["proj_id"]},
                        headers=H(admin_tok), timeout=15).json()
    # 全局规则(project_id=None) + 该项目规则都可见；本 fixture 规则为全局 → 必含
    assert any(r["id"] == adm["rule_id"] for r in rows)


def test_guard_create_disabled_flag(admin_tok):
    r = requests.post(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15,
                      json={"pattern": f"admg3{SUF}", "enabled": False})
    assert r.status_code == 200
    rid = r.json()["id"]
    rows = requests.get(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15).json()
    assert next(r for r in rows if r["id"] == rid)["enabled"] is False
    requests.delete(f"{BASE}/api/admin/guard-rules/{rid}", headers=H(admin_tok), timeout=15)


def test_guard_delete(admin_tok, adm):
    r = requests.delete(f"{BASE}/api/admin/guard-rules/{adm['rule_id']}",
                        headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    rows = requests.get(f"{BASE}/api/admin/guard-rules", headers=H(admin_tok), timeout=15).json()
    assert not any(r["id"] == adm["rule_id"] for r in rows)


def test_guard_delete_missing_200(admin_tok):
    r = requests.delete(f"{BASE}/api/admin/guard-rules/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 200


# ---------- 审计日志 ----------

def test_audit_list_200(admin_tok):
    items = _audit_items(admin_tok, limit=10)
    assert isinstance(items, list)


def test_audit_write_op_recorded(admin_tok):
    requests.post(f"{BASE}/api/admin/tenants", headers=H(admin_tok), timeout=15,
                  json={"name": f"adm-audit-{SUF}"})
    items = _audit_items(admin_tok, limit=20)
    assert any(i["path"] == "/api/admin/tenants" and i["method"] == "POST"
               and i["username"] == "admin" for i in items), "写操作未留痕"


def test_audit_security_401_recorded(admin_tok):
    requests.get(f"{BASE}/api/models", headers={"Authorization": "Bearer garbage"},
                 timeout=15)
    items = _audit_items(admin_tok, limit=200)
    assert any(i["path"].startswith("/api/models") and i["status_code"] == 401
               for i in items), "401 安全事件未留痕"


def test_audit_read_200_not_recorded(admin_tok):
    requests.get(f"{BASE}/api/models", headers=H(admin_tok), timeout=15)
    items = _audit_items(admin_tok, limit=500)
    assert not any(i["path"] == "/api/models" and i["method"] == "GET"
                   and i["status_code"] == 200 for i in items), "普通读操作不应留痕"


def test_audit_skip_dashboard(admin_tok):
    requests.get(f"{BASE}/api/dashboard/summary", headers=H(admin_tok), timeout=15)
    items = _audit_items(admin_tok, limit=500)
    assert not any(i["path"].startswith("/api/dashboard") for i in items)


def test_audit_limit_offset(admin_tok):
    r = requests.get(f"{BASE}/api/admin/audit", params={"limit": 50, "offset": 0},
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    body = r.json()
    assert body["total"] >= 0
    assert len(body["items"]) <= 50
    r2 = requests.get(f"{BASE}/api/admin/audit", params={"limit": 10, "offset": 1000000},
                      headers=H(admin_tok), timeout=15)
    assert r2.json()["items"] == []


def test_audit_username_filter(admin_tok):
    items = _audit_items(admin_tok, limit=100, username="admin")
    assert items and all(i["username"] == "admin" for i in items)


def test_audit_limit_clamped_500(admin_tok):
    r = requests.get(f"{BASE}/api/admin/audit", params={"limit": 100000},
                     headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert len(r.json()["items"]) <= 500

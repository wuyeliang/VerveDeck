# -*- coding: utf-8 -*-
"""评测中心 API：创建校验 / 真实规则评测 / 对比 / 删除

受控真实任务：1 次 rule 评测（部署 1，limit=2，约 2 次 LLM 调用）。
"""
import pytest
import requests

from conftest import BASE, H, SUF, poll, DEFAULT_PROJECT, DEPLOY_ID


@pytest.fixture(scope="module")
def ectx(admin_tok, dsctx):
    """坏版本评测（快速 failed，不占 GPU）+ 真实 rule 评测（succeeded）"""
    c = {}
    # 1) benchmark 模式：dataset_version_id=None 在 build_test_file 阶段快速失败（不起容器、不占 GPU）
    r = requests.post(f"{BASE}/api/evals", headers=H(admin_tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT, "name": f"500evalbad-{SUF}",
        "target_deployment_id": DEPLOY_ID, "benchmark": "gsm8k",
        "method": "benchmark", "limit": 1})
    assert r.status_code == 200, r.text
    c["bad_id"] = r.json()["id"]

    def bad_done():
        t = requests.get(f"{BASE}/api/evals/{c['bad_id']}",
                         headers=H(admin_tok), timeout=15).json()
        return t if t["status"] in ("succeeded", "failed") else None
    c["bad"] = poll(bad_done, timeout=120, desc="坏版本评测应快速失败")

    # 2) 真实 rule 评测
    r = requests.post(f"{BASE}/api/evals", headers=H(admin_tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT, "name": f"500eval-{SUF}",
        "target_deployment_id": DEPLOY_ID, "dataset_version_id": dsctx["alpaca_ver"],
        "method": "rule", "limit": 2})
    assert r.status_code == 200, r.text
    c["real_id"] = r.json()["id"]

    def real_done():
        t = requests.get(f"{BASE}/api/evals/{c['real_id']}",
                         headers=H(admin_tok), timeout=15).json()
        return t if t["status"] in ("succeeded", "failed") else None
    c["real"] = poll(real_done, timeout=900, desc="真实 rule 评测完成")
    return c


# ---------- 列表 / 详情 ----------

def test_list_200(admin_tok, ectx):
    rows = requests.get(f"{BASE}/api/evals", headers=H(admin_tok), timeout=15).json()
    assert isinstance(rows, list)
    assert any(t["id"] == ectx["real_id"] for t in rows)


def test_list_project_filter(admin_tok, ectx):
    rows = requests.get(f"{BASE}/api/evals",
                        params={"project_id": DEFAULT_PROJECT},
                        headers=H(admin_tok), timeout=15).json()
    assert all(t["project_id"] == DEFAULT_PROJECT for t in rows)


def test_list_scoped_tenant_a(admin_tok, tenant_a, ectx):
    """租户 A 用户看不到默认项目的评测"""
    tok = tenant_a["dev"][2]
    rows = requests.get(f"{BASE}/api/evals", headers=H(tok), timeout=15).json()
    assert not any(t["id"] == ectx["real_id"] for t in rows)


def test_get_missing_404(admin_tok):
    r = requests.get(f"{BASE}/api/evals/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_brief_fields(admin_tok, ectx):
    t = ectx["real"]
    for k in ("id", "project_id", "name", "target", "method", "status",
              "summary", "error", "benchmark", "limit", "dataset_version"):
        assert k in t, f"缺字段 {k}"


# ---------- 创建校验 ----------

def test_create_no_target_400(admin_tok, dsctx):
    r = requests.post(f"{BASE}/api/evals", headers=H(admin_tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT, "name": f"500e1-{SUF}",
        "dataset_version_id": dsctx["alpaca_ver"], "method": "rule"})
    assert r.status_code == 400


def test_create_no_version_no_bench_400(admin_tok):
    r = requests.post(f"{BASE}/api/evals", headers=H(admin_tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT, "name": f"500e2-{SUF}",
        "target_deployment_id": DEPLOY_ID, "method": "rule"})
    assert r.status_code == 400


def test_create_bad_benchmark_400(admin_tok):
    r = requests.post(f"{BASE}/api/evals", headers=H(admin_tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT, "name": f"500e3-{SUF}",
        "target_deployment_id": DEPLOY_ID, "benchmark": "not-a-bench"})
    assert r.status_code == 400


def test_create_judge_without_judge_deploy_400(admin_tok, dsctx):
    r = requests.post(f"{BASE}/api/evals", headers=H(admin_tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT, "name": f"500e4-{SUF}",
        "target_deployment_id": DEPLOY_ID,
        "dataset_version_id": dsctx["alpaca_ver"], "method": "judge"})
    assert r.status_code == 400


def test_create_cross_project_403(admin_tok, tenant_a, dsctx):
    tok = tenant_a["dev"][2]
    r = requests.post(f"{BASE}/api/evals", headers=H(tok), timeout=30, json={
        "project_id": DEFAULT_PROJECT, "name": f"500e5-{SUF}",
        "target_deployment_id": DEPLOY_ID,
        "dataset_version_id": dsctx["alpaca_ver"], "method": "rule"})
    assert r.status_code in (403, 404)


# ---------- 真实评测结果 ----------

def test_bad_version_failed_with_error(admin_tok, ectx):
    t = ectx["bad"]
    assert t["status"] == "failed", f"benchmark 评测应快速 failed: {t['status']} {t['error']}"
    assert t["error"], "失败原因不可为空"


def test_create_missing_version_404(admin_tok):
    # 版本存在性预检：不存在的 dataset_version_id 应在创建时 404（而非入库后 FK 500）
    r = requests.post(f"{BASE}/api/evals", headers=H(admin_tok), timeout=15, json={
        "project_id": DEFAULT_PROJECT, "name": f"500evalmv-{SUF}",
        "target_deployment_id": DEPLOY_ID, "dataset_version_id": 999999,
        "method": "rule"})
    assert r.status_code == 404, r.text[:200]


def test_real_rule_succeeded(admin_tok, ectx):
    t = ectx["real"]
    assert t["status"] == "succeeded", f"真实 rule 评测应成功: {t.get('error')}"


def test_real_summary_nonempty(admin_tok, ectx):
    t = ectx["real"]
    assert t["summary"], "summary 不可为空"


def test_real_details(admin_tok, ectx):
    body = requests.get(f"{BASE}/api/evals/{ectx['real_id']}",
                        headers=H(admin_tok), timeout=15).json()
    assert isinstance(body["details"], list)
    assert len(body["details"]) <= 500


# ---------- 对比 / 删除 ----------

def test_compare_empty(admin_tok):
    r = requests.post(f"{BASE}/api/evals/compare", headers=H(admin_tok), timeout=15,
                      json={"ids": []})
    assert r.status_code == 200
    assert r.json()["items"] == []


def test_compare_with_ids(admin_tok, ectx):
    r = requests.post(f"{BASE}/api/evals/compare", headers=H(admin_tok), timeout=15,
                      json={"ids": [ectx["real_id"]]})
    assert r.status_code == 200
    assert len(r.json()["items"]) == 1
    assert r.json()["items"][0]["id"] == ectx["real_id"]


def test_delete_real_succeeded(admin_tok, ectx):
    r = requests.delete(f"{BASE}/api/evals/{ectx['real_id']}",
                        headers=H(admin_tok), timeout=15)
    assert r.status_code == 200
    assert requests.get(f"{BASE}/api/evals/{ectx['real_id']}",
                        headers=H(admin_tok), timeout=15).status_code == 404


def test_delete_missing_404(admin_tok):
    r = requests.delete(f"{BASE}/api/evals/999999", headers=H(admin_tok), timeout=15)
    assert r.status_code == 404


def test_delete_bad_failed(admin_tok, ectx):
    r = requests.delete(f"{BASE}/api/evals/{ectx['bad_id']}",
                        headers=H(admin_tok), timeout=15)
    assert r.status_code == 200


def test_cross_project_get_403(admin_tok, tenant_a, ectx):
    tok = tenant_a["dev"][2]
    r = requests.get(f"{BASE}/api/evals/{ectx['bad_id']}", headers=H(tok), timeout=15)
    assert r.status_code in (403, 404)

"""评测中心：通用榜单 + 业务自定义测试集；规则匹配 / LLM-as-a-Judge / 前后对比"""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from ..database import get_db, SessionLocal
from ..security import get_current_user, can_access_project
from ..models import EvalTask, Project, DatasetVersion, Deployment, ModelAsset, User
from ..services import eval_service as ev

router = APIRouter(prefix="/api/evals", tags=["evals"])

BENCHMARKS = ["gsm8k", "ceval", "arc", "cmmlu", "human_eval", "mbpp"]


def _check(db, pid, user):
    p = db.get(Project, pid)
    if not p or not can_access_project(user, db, p):
        raise HTTPException(403, "无权限")


def _brief(t: EvalTask, db: Session) -> dict:
    dep = db.get(Deployment, t.target_deployment_id) if t.target_deployment_id else None
    asset = db.get(ModelAsset, t.target_asset_id) if t.target_asset_id else None
    ver = db.get(DatasetVersion, t.dataset_version_id) if t.dataset_version_id else None
    return {
        "id": t.id, "project_id": t.project_id, "name": t.name,
        "target": dep.name if dep else (asset.name if asset else ""),
        "target_deployment_id": t.target_deployment_id,
        "target_asset_id": t.target_asset_id,
        "dataset_version": f"v{ver.version}" if ver else "",
        "benchmark": t.benchmark, "method": t.method, "limit": t.limit,
        "status": t.status, "summary": t.summary or {}, "error": t.error,
        "created_at": str(t.created_at),
        "finished_at": str(t.finished_at) if t.finished_at else "",
    }


class EvalReq(BaseModel):
    project_id: int
    name: str
    target_deployment_id: int | None = None
    target_asset_id: int | None = None  # quick-serve
    dataset_version_id: int | None = None  # 自定义测试集（benchmark 时忽略）
    benchmark: str = ""  # gsm8k/ceval/arc/...
    method: str = "rule"  # rule/judge/both/benchmark
    judge_deployment_id: int | None = None
    judge_model: str = ""
    limit: int = 100


@router.post("")
def create_eval(req: EvalReq, db=Depends(get_db), user=Depends(get_current_user)):
    _check(db, req.project_id, user)
    if not req.benchmark and not req.dataset_version_id:
        raise HTTPException(400, "需指定 benchmark 或自定义测试集 dataset_version_id")
    if req.dataset_version_id and not db.get(DatasetVersion, req.dataset_version_id):
        raise HTTPException(404, "数据集版本不存在")
    if req.benchmark and req.benchmark not in BENCHMARKS:
        raise HTTPException(400, f"benchmark 需为 {BENCHMARKS} 之一")
    if not req.target_deployment_id and not req.target_asset_id:
        raise HTTPException(400, "需指定被评测模型（运行中部署 或 模型资产 quick-serve）")
    if req.method in ("judge", "both") and not req.judge_deployment_id:
        raise HTTPException(400, "judge/both 模式需指定裁判模型部署")
    if req.method == "benchmark" and req.benchmark:
        req.method = "benchmark"
    t = EvalTask(
        project_id=req.project_id, name=req.name,
        target_deployment_id=req.target_deployment_id, target_asset_id=req.target_asset_id,
        dataset_version_id=req.dataset_version_id, benchmark=req.benchmark,
        method=req.method, judge_deployment_id=req.judge_deployment_id,
        judge_model=req.judge_model, limit=req.limit, status="pending", created_by=user.id,
    )
    db.add(t)
    db.commit()
    db.refresh(t)
    # 同步启动（quick-serve 可能耗时，放后台线程）
    import threading
    def _launch():
        db2 = SessionLocal()
        try:
            task = db2.get(EvalTask, t.id)
            task.status = "running"
            db2.commit()
            ev.launch(task)
        except Exception as e:
            task2 = db2.get(EvalTask, t.id)
            task2.status = "failed"
            task2.error = str(e)[:4000]
            db2.commit()
        finally:
            db2.close()
    threading.Thread(target=_launch, daemon=True).start()
    return _brief(t, db)


@router.get("")
def list_evals(project_id: int | None = None, db=Depends(get_db), user=Depends(get_current_user)):
    if project_id:
        _check(db, project_id, user)
        q = db.query(EvalTask).filter(EvalTask.project_id == project_id)
    else:
        pids = [p.id for p in db.query(Project).all() if can_access_project(user, db, p)]
        q = db.query(EvalTask).filter(EvalTask.project_id.in_(pids or [0]))
    rows = q.order_by(EvalTask.id.desc()).limit(200).all()
    return [_brief(t, db) for t in rows]


@router.get("/{tid}")
def get_eval(tid: int, db=Depends(get_db), user=Depends(get_current_user)):
    t = db.get(EvalTask, tid)
    if not t:
        raise HTTPException(404, "评测不存在")
    _check(db, t.project_id, user)
    import json, os
    details = []
    p = os.path.join(ev.eval_dir(t.id), "result.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                data = json.load(f)
            details = data.get("details", [])[:500]
        except Exception:
            pass
    return {**_brief(t, db), "details": details}


@router.get("/{tid}/report")
def download_report(tid: int, db=Depends(get_db), user=Depends(get_current_user)):
    """评测报告下载（Markdown）：任务信息 + 汇总指标 + 明细表"""
    from fastapi.responses import PlainTextResponse
    import json, os
    t = db.get(EvalTask, tid)
    if not t:
        raise HTTPException(404, "评测不存在")
    _check(db, t.project_id, user)
    brief = _brief(t, db)
    summary = t.summary or {}
    details = []
    p = os.path.join(ev.eval_dir(t.id), "result.json")
    if os.path.exists(p):
        try:
            with open(p, encoding="utf-8") as f:
                details = json.load(f).get("details", [])[:100]
        except Exception:
            pass
    L = [
        f"# 评测报告 · {t.name}（#{t.id}）", "",
        f"- 状态：{t.status}",
        f"- 被评对象：{brief['target'] or '-'}",
        f"- 数据集/榜单：{t.benchmark or brief['dataset_version'] or '-'}",
        f"- 评测方式：{t.method}",
        f"- 样本上限：{t.limit}",
        f"- 创建时间：{t.created_at}",
        f"- 完成时间：{t.finished_at or '-'}",
    ]
    if t.error:
        L += ["", "## 错误", "", "```", t.error[:2000], "```"]
    if summary:
        L += ["", "## 汇总指标", ""]
        for k, v in summary.items():
            L.append(f"- **{k}**: {v}")
    if details:
        L += ["", "## 明细（前 100 条）", "",
              "| # | Prompt | 参考 | 预测 | F1 | EM | Judge | 错误 |",
              "|---|---|---|---|---|---|---|---|"]
        for d in details:
            esc = lambda s: str(s or "").replace("|", "\\|").replace("\n", " ")[:80]
            rule = d.get("rule", {}) or {}
            judge = d.get("judge", {}) or {}
            L.append("| {i} | {p} | {r} | {q} | {f1} | {em} | {js} | {e} |".format(
                i=d.get("index", ""), p=esc(d.get("prompt")), r=esc(d.get("reference")),
                q=esc(d.get("pred")), f1=rule.get("f1", "-"), em=rule.get("exact", "-"),
                js=judge.get("score", "-"), e=esc(d.get("error"))))
    return PlainTextResponse("\n".join(L) + "\n",
                             media_type="text/markdown; charset=utf-8",
                             headers={"Content-Disposition":
                                      f'attachment; filename="eval-{tid}-report.md"'})


@router.post("/compare")
def compare(req: dict, db=Depends(get_db), user=Depends(get_current_user)):
    """前后效果对比：传入 eval id 列表，返回汇总（前端画对比图）"""
    ids = req.get("ids", [])
    out = []
    for tid in ids[:10]:
        t = db.get(EvalTask, tid)
        if t:
            _check(db, t.project_id, user)
            out.append(_brief(t, db))
    return {"items": out}


@router.delete("/{tid}")
def delete_eval(tid: int, db=Depends(get_db), user=Depends(get_current_user)):
    t = db.get(EvalTask, tid)
    if not t:
        raise HTTPException(404, "评测不存在")
    _check(db, t.project_id, user)
    if t.status in ("pending", "running"):
        raise HTTPException(400, "运行中的评测不可删除")
    import shutil
    db.delete(t)
    db.commit()
    # 清理评测产物目录（result.json/日志/临时文件）
    shutil.rmtree(ev.eval_dir(tid), ignore_errors=True)
    return {"ok": True}


class BatchDeleteReq(BaseModel):
    ids: list[int]


@router.post("/batch-delete")
def batch_delete_evals(req: BatchDeleteReq, db=Depends(get_db), user=Depends(get_current_user)):
    """批量删除：逐个执行单删逻辑（运行中的评测跳过并报错），返回部分结果"""
    deleted: list[int] = []
    failed: list[dict] = []
    for tid in req.ids:
        try:
            delete_eval(tid, db=db, user=user)
            deleted.append(tid)
        except HTTPException as e:
            db.rollback()
            failed.append({"id": tid, "error": str(e.detail)})
    return {"deleted": deleted, "failed": failed}

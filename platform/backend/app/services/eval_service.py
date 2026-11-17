"""评测任务服务

架构：每个评测任务 = 一个独立 pause 占位容器（CPU 即可，通过 API 调目标模型），
exec 运行 runner.py（规则匹配 / LLM-as-a-Judge / evalscope 榜单）。
"""
import json
import os
import time

from ..config import settings
from ..models import EvalTask, Deployment, ModelAsset, Dataset, DatasetVersion
from ..database import SessionLocal
from . import taskcontainer as tc
from . import dataset_service as ds
from . import upgrade_service
from .inference_service import deployment_endpoint, ensure_serving


def eval_dir(task_id: int) -> str:
    return os.path.join(settings.data_dir, "evals", str(task_id))


def build_test_file(task: EvalTask, db) -> str:
    """把测试集版本转成 {prompt, reference} jsonl"""
    ver = db.get(DatasetVersion, task.dataset_version_id)
    dset = db.get(Dataset, ver.dataset_id)
    rows = ds.read_jsonl(ds.ensure_version_local(ds.dataset_repo_dir(dset.project_id, dset.name), ver.version))
    out = os.path.join(eval_dir(task.id), "test.jsonl")
    os.makedirs(eval_dir(task.id), exist_ok=True)
    lines = []
    for r in rows:
        if ver.fmt == "dpo":
            prompt, ref = r.get("prompt", ""), r.get("chosen", "")
        else:
            prompt = (r.get("instruction", "") + ("\n" + r.get("input", "") if r.get("input") else ""))
            ref = r.get("output", "")
        if prompt:
            lines.append(json.dumps({"prompt": prompt, "reference": ref}, ensure_ascii=False))
    with open(out, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return out


def launch(task: EvalTask) -> None:
    db = SessionLocal()
    task_id = None
    task_json = None
    try:
        # 传入的 task 挂在调用方会话上；bind 到本会话后，后续修改（如 work_dir）
        # 才能随本会话 commit 落库，否则调度器轮询时 work_dir 为 None，失败原因无法回读
        task = db.merge(task)
        task_id = task.id
        # 目标模型端点：运行中的部署 或 资产快速拉起（quick-serve）
        target_url, target_model = "", ""
        if task.target_deployment_id:
            dep = db.get(Deployment, task.target_deployment_id)
            if not dep or dep.status != "running" or not dep.endpoint:
                raise RuntimeError("目标部署未运行，请先启动该推理部署")
            target_url, target_model = deployment_endpoint(dep), dep.name
        elif task.target_asset_id:
            asset = db.get(ModelAsset, task.target_asset_id)
            target_url, target_model = ensure_serving(asset, db)
        judge_url, judge_model = "", ""
        if task.method in ("judge", "both") and task.judge_deployment_id:
            jdep = db.get(Deployment, task.judge_deployment_id)
            judge_url, judge_model = deployment_endpoint(jdep), task.judge_model or jdep.name
        test_file = build_test_file(task, db)
        task.work_dir = eval_dir(task_id)
        db.commit()
        # commit 会 expire 全部属性（expire_on_commit）；session 关闭后访问 ORM 属性
        # 会触发 detached refresh 崩溃，因此 task.json 必须在关闭前装配
        task_json = {
            "mode": "benchmark" if task.benchmark else task.method,
            "target_url": target_url,
            "target_model": target_model,
            "test_file": test_file,
            "judge_url": judge_url,
            "judge_model": judge_model,
            "benchmark": task.benchmark,
            "limit": task.limit,
            "out_dir": eval_dir(task_id),
        }
    finally:
        db.close()
    if task_json is None:
        raise RuntimeError("评测任务上下文初始化失败")

    os.makedirs(eval_dir(task_id), exist_ok=True)
    with open(os.path.join(eval_dir(task_id), "task.json"), "w", encoding="utf-8") as f:
        json.dump(task_json, f, ensure_ascii=False)

    name = f"ft-eval-{task_id}"
    tc.remove_container(name, force=True)
    cid = tc.create_container(
        name=name, image=upgrade_service.current_image("evalscope"), gpus=0, cpus=4, mem_gb=8,
        labels={"fintune.job": str(task_id), "fintune.type": "eval"},
    )
    # task.json 由 backend 写到宿主机 /data/evals/<id>/（容器经挂载可见）；
    # 镜像只内置 /workspace/runner.py，需把 task.json 拷到 runner 的读取位置
    cmd = ["bash", "-lc",
           f"cp /data/evals/{task_id}/task.json /workspace/task.json && "
           f"python /workspace/runner.py 2>&1"]
    tc.start_task(f"eval-{task_id}", cid, cmd, os.path.join(eval_dir(task_id), "run.log"))
    db = SessionLocal()
    try:
        t = db.get(EvalTask, task_id)
        t.container_id = cid
        t.status = "running"
        db.commit()
    finally:
        db.close()


def collect_result(task: EvalTask) -> dict | None:
    p = os.path.join(eval_dir(task.id), "result.json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def cleanup(task: EvalTask) -> None:
    if task.container_id:
        tc.remove_container(f"ft-eval-{task.id}", force=True)

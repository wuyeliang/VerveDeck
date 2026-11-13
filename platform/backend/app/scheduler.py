"""算力调度器（后台线程）

职责：
1. GPU 节点心跳采集 + 故障自愈（离线判定、任务收敛）
2. 多任务排队 + 优先级调度（GPU 容量检查 → 准入 → 启动任务容器）
3. 任务状态收敛（exec 退出码 → 成功/失败 + 产物注册 + 容器清理）
4. 基座模型下载队列（backend 线程内 huggingface-cli，不起容器）

约定：仅 训练/推理/评测 三大类任务新起容器（pause 占位 + exec 通信）。
"""
import logging
import os
import re
import threading
import time

from sqlalchemy import func

from .config import settings
from .database import SessionLocal
from .models import TrainingJob, EvalTask, Deployment, ModelAsset, GpuNode, GpuMetric, QuantTask
from .services import gpu_service
from .services import taskcontainer as tc
from .services import training_service, eval_service, inference_service, quantize_service

log = logging.getLogger("scheduler")


def _node(db):
    return db.query(GpuNode).filter_by(name=settings.node_name).first()


def _gpu_in_use(db) -> int:
    used = 0
    for j in db.query(TrainingJob).filter(TrainingJob.status == "running").all():
        used += j.gpu_count or 1
    for d in db.query(Deployment).filter(Deployment.status.in_(["running", "creating"])).all():
        used += (d.tensor_parallel or 1) * max(d.replicas, 1)
    for q in db.query(QuantTask).filter(QuantTask.status == "running").all():
        used += q.gpu_count or 1
    return used


def _tail(path: str, n: int = 30) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 8000))
            data = f.read().decode("utf-8", "ignore")
        return "\n".join(data.splitlines()[-n:])
    except Exception:
        return ""


def _finalize_training(db, job: TrainingJob, success: bool, error: str = ""):
    from datetime import datetime
    job.status = "succeeded" if success else "failed"
    job.error = error[:4000]
    job.finished_at = datetime.utcnow()
    if success:
        training_service.register_artifact(job)
    tc.remove_container(job.container_name, force=True)
    db.commit()


def _finalize_eval(db, task: EvalTask, success: bool, error: str = ""):
    from datetime import datetime
    task.status = "succeeded" if success else "failed"
    task.error = error[:4000]
    task.finished_at = datetime.utcnow()
    if success:
        res = eval_service.collect_result(task)
        if res:
            task.summary = res.get("summary", {})
            if "details" in res:
                task.summary["detail_count"] = len(res["details"])
    eval_service.cleanup(task)
    db.commit()


def _reconcile_training(db):
    for job in db.query(TrainingJob).filter(TrainingJob.status == "running").all():
        st = tc.task_state(f"train-{job.id}")
        if st["known"] and st["done"]:
            code = st["code"]
            _finalize_training(db, job, code == 0,
                               "" if code == 0 else f"exit code {code}\n" + _tail(f"{job.work_dir}/run.log"))
            log.info("training job %s -> %s", job.id, job.status)
        elif not st["known"]:
            # backend 重启导致 exec 线程丢失
            alive = tc.container_alive(job.container_name)
            if not alive:
                _finalize_training(db, job, False, "容器不存在（可能已清理/节点故障），请断点续训")
            else:
                # 容器还在但执行线程丢失：标记失败并清理（checkpoint 仍在，可续训）
                _finalize_training(db, job, False, "调度器重启，任务上下文丢失；checkpoint 已保留，请断点续训")
            log.warning("training job %s reconciled (lost context)", job.id)


def _finalize_quant(db, task: QuantTask, success: bool, error: str = ""):
    from datetime import datetime
    task.status = "succeeded" if success else "failed"
    task.error = error[:4000]
    task.finished_at = datetime.utcnow()
    if success:
        asset = quantize_service.register_artifact(task)
        if not asset:
            task.status = "failed"
            task.error = "量化执行完成但未找到产物（out 目录为空），请查看日志"
    quantize_service.cleanup(task)
    db.commit()


def _reconcile_quant(db):
    for task in db.query(QuantTask).filter(QuantTask.status == "running").all():
        st = tc.task_state(f"quant-{task.id}")
        if st["known"] and st["done"]:
            code = st["code"]
            _finalize_quant(db, task, code == 0,
                            "" if code == 0 else f"exit code {code}\n" + _tail(f"{quantize_service.task_dir(task.id)}/run.log"))
            log.info("quant task %s -> %s", task.id, task.status)
        elif not st["known"]:
            # backend 重启导致 exec 线程丢失
            _finalize_quant(db, task, False, "调度器重启，量化上下文丢失")
            log.warning("quant task %s reconciled (lost context)", task.id)


def _reconcile_eval(db):
    for task in db.query(EvalTask).filter(EvalTask.status == "running").all():
        st = tc.task_state(f"eval-{task.id}")
        if st["known"] and st["done"]:
            res = eval_service.collect_result(task)
            _finalize_eval(db, task, st["code"] == 0 and res is not None,
                           "" if res else _tail(f"{task.work_dir}/run.log"))
            log.info("eval task %s -> %s", task.id, task.status)
        elif not st["known"]:
            _finalize_eval(db, task, False, "调度器重启，评测上下文丢失")


def _reconcile_deployments(db):
    for dep in db.query(Deployment).filter(Deployment.status.in_(["running", "creating"])).all():
        alive = all(tc.container_alive(n) for n in
                    (dep.container_ids or [f"ft-infer-{dep.id}"]))
        if not alive:
            dep.status = "failed"
            dep.error = "推理容器异常退出（节点故障或 OOM），请重新启动"
            db.commit()
            log.warning("deployment %s lost", dep.name)
            continue
        if dep.status == "creating":
            ok = inference_service.wait_ready(dep, timeout=10)
            if ok:
                dep.status = "running"
                dep.error = ""  # 清除历史错误，避免 DB 显示陈旧 error
                db.commit()
                log.info("deployment %s ready", dep.name)
        elif dep.status == "running" and dep.error:
            # 容器确认存活但 DB 残留历史 error（如上次 lost 事件），清掉
            dep.error = ""
            db.commit()


def _admit_jobs(db):
    node = _node(db)
    if not node or node.status != "online" or not node.gpu_count:
        return
    free = node.gpu_count - _gpu_in_use(db)
    if free <= 0:
        return
    queued = (db.query(TrainingJob)
              .filter(TrainingJob.status == "queued")
              .order_by(TrainingJob.priority.desc(), TrainingJob.id.asc()).all())
    for job in queued:
        need = job.gpu_count or 1
        if need <= free:
            try:
                training_service.launch(job)
                free -= need
                log.info("admitted training job %s (%d gpus)", job.id, need)
            except Exception as e:
                job.status = "failed"
                job.error = f"启动失败: {e}"
                db.commit()
                log.exception("launch job %s failed", job.id)
    # 量化任务（每任务固定 1 卡）：训练任务之后按 id 升序补位
    queued_q = (db.query(QuantTask)
                .filter(QuantTask.status == "pending")
                .order_by(QuantTask.id.asc()).all())
    for task in queued_q:
        if (task.gpu_count or 1) <= free:
            try:
                quantize_service.launch(task)
                free -= task.gpu_count or 1
                log.info("admitted quant task %s", task.id)
            except Exception as e:
                task.status = "failed"
                task.error = f"启动失败: {e}"
                db.commit()
                log.exception("launch quant task %s failed", task.id)


def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def _model_downloads(db):
    for asset in db.query(ModelAsset).filter(ModelAsset.status == "pending_download").all():
        if not asset.hf_id:
            continue
        key = f"dl-{asset.id}"
        st = tc.task_state(key)
        if st["known"] and st["done"]:
            ok = st["code"] == 0
            asset.status = "ready" if ok else "failed"
            if ok and not asset.size_bytes:
                asset.size_bytes = _dir_size(asset.path)
            db.commit()
            continue
        if st["known"]:
            continue  # 下载中
        # backend 线程内下载（不起容器）
        def _dl(a=asset):
            try:
                if a.hf_id.startswith("ms:"):
                    # ModelScope 仓库（搜索源=ModelScope 时自动加前缀）
                    from modelscope.hub.snapshot_download import snapshot_download as ms_dl
                    ms_dl(a.hf_id[3:], local_dir=a.path)
                    code = 0
                else:
                    from huggingface_hub import snapshot_download
                    snapshot_download(a.hf_id, local_dir=a.path,
                                      endpoint=settings.hf_endpoint)
                    code = 0
            except Exception as e:
                log.error("download %s failed: %s", a.hf_id, e)
                code = 1
            with tc._lock:
                tc.RUNNERS[key] = {"done": True, "code": code, "thread": None}
        tc.RUNNERS[key] = {"done": False, "code": None, "thread": None}
        threading.Thread(target=_dl, daemon=True).start()
        log.info("model download started %s -> %s", asset.hf_id, asset.path)


def _gc_metrics(db):
    cutoff = int(time.time()) - 6 * 3600
    try:
        db.query(GpuMetric).filter(GpuMetric.ts < cutoff).delete()
        db.commit()
    except Exception:
        db.rollback()


class SchedulerThread(threading.Thread):
    def __init__(self):
        super().__init__(name="scheduler", daemon=True)
        self._stop = threading.Event()

    def run(self):
        log.info("scheduler started")
        while not self._stop.is_set():
            db = SessionLocal()
            try:
                gpu_service.tick()
                _reconcile_training(db)
                _reconcile_quant(db)
                _reconcile_eval(db)
                _reconcile_deployments(db)
                _admit_jobs(db)
                _model_downloads(db)
                if int(time.time()) % 3600 < 10:
                    _gc_metrics(db)
            except Exception:
                db.rollback()
                log.exception("scheduler tick error")
            finally:
                db.close()
            self._stop.wait(5)

    def stop(self):
        self._stop.set()


_scheduler: SchedulerThread | None = None


def start_scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = SchedulerThread()
        _scheduler.start()

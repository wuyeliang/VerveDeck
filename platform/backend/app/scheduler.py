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
from .models import (TrainingJob, EvalTask, Deployment, ModelAsset, GpuNode,
                     QuantTask, AlertEvent)
from .services import gpu_service
from .services import taskcontainer as tc
from .services import training_service, eval_service, inference_service, quantize_service
from .services import image_ops
from .services import alert_service
from .services import upgrade_service

log = logging.getLogger("scheduler")


def _node(db):
    return db.query(GpuNode).filter_by(name=settings.node_name).first()


def _gpu_in_use(db) -> int:
    used = 0
    for j in db.query(TrainingJob).filter(TrainingJob.status == "running").all():
        used += training_service.job_gpu_need(j)
    for d in db.query(Deployment).filter(Deployment.status.in_(["running", "creating"])).all():
        used += (d.tensor_parallel or 1) * max(d.replicas, 1)
    for q in db.query(QuantTask).filter(QuantTask.status == "running").all():
        used += q.gpu_count or 1
    return used


def _free_gpus(db) -> int:
    """跨在线节点的空闲卡数（= 全在线节点卡数之和 − 全局占用）。

    单主机部署（仅 node-1 在线）即本节点空闲，行为与旧版一致；注册更多在线节点
    后自动扩容调度池。多机训练任务的 rank 容器当前均落在本地宿主（单主机），
    故此池即其可用上限。
    """
    total = sum((n.gpu_count or 0) for n in
                db.query(GpuNode).filter(GpuNode.status == "online").all())
    return max(0, total - _gpu_in_use(db))


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
    else:
        alert_service.trigger_event(db, "train_failed", f"train:{job.id}",
                                    f"训练任务失败：{job.name}",
                                    f"ID {job.id} · 阶段 {job.stage} · {job.error[:300]}")
    # 清理全部 rank 容器（单卡 = 单容器，行为不变）
    for name in training_service.rank_container_names(job):
        tc.remove_container(name, force=True)
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
    else:
        alert_service.trigger_event(db, "eval_failed", f"eval:{task.id}",
                                    f"评测任务失败：{task.name}",
                                    f"ID {task.id} · {task.error[:300]}")
    eval_service.cleanup(task)
    db.commit()


def _reconcile_training(db):
    for job in db.query(TrainingJob).filter(TrainingJob.status == "running").all():
        keys = training_service.rank_task_keys(job)
        logs = training_service.rank_log_paths(job)
        states = [tc.task_state(k) for k in keys]
        known_all = all(s["known"] for s in states)
        done_all = all(s["done"] for s in states)
        if known_all and done_all:
            # 所有 rank 都完成：任一非零退出即整体失败
            codes = [s["code"] for s in states]
            if all(c == 0 for c in codes):
                _finalize_training(db, job, True, "")
            elif len(codes) == 1:
                # 单卡：沿用原错误文案（回归不变）
                _finalize_training(db, job, False, f"exit code {codes[0]}\n" + _tail(logs[0]))
            else:
                bad = [(i, k, c) for i, (k, c) in enumerate(zip(keys, codes)) if c != 0]
                tail = "\n".join(f"[{k}] exit {c}\n{_tail(logs[i], 20)}" for i, k, c in bad)
                _finalize_training(db, job, False, "分布式训练失败：\n" + tail)
            log.info("training job %s -> %s", job.id, job.status)
        elif not known_all:
            # backend 重启导致部分/全部 exec 线程丢失
            alive = any(tc.container_alive(n) for n in training_service.rank_container_names(job))
            if not alive:
                _finalize_training(db, job, False, "容器不存在（可能已清理/节点故障），请断点续训")
            else:
                # 容器还在但执行线程丢失：标记失败并清理（checkpoint 仍在，可续训）
                _finalize_training(db, job, False, "调度器重启，任务上下文丢失；checkpoint 已保留，请断点续训")
            log.warning("training job %s reconciled (lost context)", job.id)
        # else：部分 rank 仍在运行 → 本 tick 不收敛，下个 tick 再判断


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
    if task.status == "failed":
        alert_service.trigger_event(db, "quant_failed", f"quant:{task.id}",
                                    f"量化任务失败：{task.name}",
                                    f"ID {task.id} · {task.error[:300]}")
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
        # 镜像懒加载守卫：引擎镜像未就绪时保持 creating，勿按"容器丢失"判失败
        img = upgrade_service.current_image("vllm") if dep.engine == "vllm" \
            else upgrade_service.current_image("sglang")
        ist = image_ops.ensure_image(img)
        if ist != "ready":
            dep.status = "creating"
            new_err = (f"引擎镜像拉取失败: {image_ops.image_status(img).get('error', '')}"
                       if ist == "failed"
                       else f"引擎镜像 {img} 下载中，完成后自动创建容器…")
            if ist == "failed" or dep.error != new_err:
                dep.error = new_err
                db.commit()
            continue
        alive = all(tc.container_alive(n) for n in
                    (dep.container_ids or [f"ft-infer-{dep.id}"]))
        if not alive:
            was_running = dep.status == "running"
            dep.status = "failed"
            dep.error = "推理容器异常退出（节点故障或 OOM），请重新启动"
            db.commit()
            log.warning("deployment %s lost", dep.name)
            if was_running:
                alert_service.trigger_event(db, "infer_down", f"deploy:{dep.id}",
                                            f"推理服务不可用：{dep.name}",
                                            "推理容器异常退出（节点故障或 OOM），服务已中断，请重新启动")
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
    # 跨节点准入：以全部在线节点的总空闲卡数为调度池（单主机部署即本节点，行为不变）
    free = _free_gpus(db)
    if free <= 0:
        return
    queued = (db.query(TrainingJob)
              .filter(TrainingJob.status == "queued")
              .order_by(TrainingJob.priority.desc(), TrainingJob.id.asc()).all())
    for job in queued:
        need = training_service.job_gpu_need(job)  # 总卡数 = 节点数 × 每节点卡数
        if need <= free:
            try:
                training_service.launch(job)
                free -= need
                log.info("admitted training job %s (%d gpus / %d nodes)",
                         job.id, need, job.node_count or 1)
            except image_ops.ImageNotReady as e:
                # 镜像懒加载：保持排队，等后台拉取完成后下个 tick 自动重试
                job.status = "queued"
                if job.error != str(e):
                    job.error = str(e)
                    db.commit()
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
            except image_ops.ImageNotReady as e:
                task.status = "pending"
                if task.error != str(e):
                    task.error = str(e)
                    db.commit()
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
    """保留策略：告警事件 7d（指标数据在 Prometheus，保留期由 prometheus.yml 控制）"""
    now = int(time.time())
    try:
        db.query(AlertEvent).filter(AlertEvent.ts < now - 7 * 24 * 3600).delete()
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
                gpu_service.tick()          # 节点心跳（内部含 node_offline/online 事件；指标由 Prometheus 采集）
                _reconcile_training(db)
                _reconcile_quant(db)
                _reconcile_eval(db)
                _reconcile_deployments(db)  # 容器丢失判 failed（含 infer_down 事件）
                _admit_jobs(db)
                _model_downloads(db)
                inference_service.scrape_metrics(db)  # 引擎探活兜底（约 30s 一次，内部自节流）
                upgrade_service.converge(db)          # backend/web 自升级任务收敛（蓝绿切换兜底）
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

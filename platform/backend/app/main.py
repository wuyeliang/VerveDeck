"""VerveDeck 大模型微调训练平台 - 后端入口"""
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import Base, engine, SessionLocal
from . import models  # noqa: F401  注册 ORM
from .security import hash_password
from .models import User, Tenant, Project, GpuNode, GuardRule, AlertRule
from .audit import AuditMiddleware
from .scheduler import start_scheduler
from .api import (auth, admin, datasets, models_api, training, evals,
                  inference, cluster, dashboard, quantize, storage, logs, images)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

APP_VERSION = "1.1"
APP_LICENSE = "Apache-2.0"
APP_GITHUB = "https://github.com/wuyeliang/VerveDeck"

# 安全加固：公网部署可设 API_DOCS_ENABLED=false 关闭接口文档（防接口清单泄露）
_DOCS = settings.api_docs_enabled
app = FastAPI(title="VerveDeck 大模型微调训练平台", version=APP_VERSION,
              docs_url="/docs" if _DOCS else None,
              redoc_url=None,
              openapi_url="/openapi.json" if _DOCS else None)
_CORS = [o.strip() for o in settings.cors_origins.split(",") if o.strip()] or ["*"]
app.add_middleware(CORSMiddleware, allow_origins=_CORS, allow_methods=["*"],
                   allow_headers=["*"])
app.add_middleware(AuditMiddleware)


def _drop_legacy_metric_tables():
    """监控告警全新架构：GpuMetric/SystemMetric/InferenceMetric 三张旧 DB 指标表
    已由 Prometheus（dcgm-exporter/node-exporter/引擎 /metrics）取代，旧库直接删表。幂等。"""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(engine)
        for t in ("gpu_metrics", "system_metrics", "inference_metrics"):
            if t in insp.get_table_names():
                with engine.begin() as conn:
                    conn.execute(text(f"DROP TABLE IF EXISTS {t}"))
                log.info("legacy metric table dropped: %s", t)
    except Exception as e:
        log.warning("legacy metric tables drop failed: %s", e)


def _migrate_columns():
    """旧库补列（create_all 只建缺失表、不补已有表的列）。幂等。"""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(engine)
        json_type = "JSON" if engine.dialect.name == "sqlite" else "JSONB"
        plan = {
            "datasets": [
                ("kind", "VARCHAR(16) NOT NULL DEFAULT 'train'"),
                ("scenario", "VARCHAR(16) NOT NULL DEFAULT 'text'"),
            ],
            "deployments": [
                ("description", "TEXT NOT NULL DEFAULT ''"),
                # 推理高级参数（#96）：JSON 兜底，留空=引擎默认，存量部署行为不变
                ("advanced", f"{json_type} NOT NULL DEFAULT '{{}}'"),
            ],
            "users": [("must_change_password", "BOOLEAN NOT NULL DEFAULT 'false'")],
            "model_assets": [
                ("category", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("tags", f"{json_type} NOT NULL DEFAULT '[]'"),
            ],
            "audit_logs": [("action", "VARCHAR(64) NOT NULL DEFAULT ''")],
            "api_keys": [("model_ids", f"{json_type} NOT NULL DEFAULT '[]'")],
            # 告警规则分类 + 事件人工确认（存量库补列；新库由 create_all 建出）
            "alert_rules": [("category", "VARCHAR(64) NOT NULL DEFAULT ''")],
            "alert_events": [
                ("acked", "BOOLEAN NOT NULL DEFAULT 'false'"),
                ("acked_by", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("acked_at", "TIMESTAMP"),
            ],
            "training_jobs": [
                # 多机多卡/单机多卡分布式训练（#60）：节点数 × 每节点卡数 + NCCL 通信环境
                ("node_count", "INTEGER NOT NULL DEFAULT 1"),
                ("nproc_per_node", "INTEGER NOT NULL DEFAULT 1"),
                ("nccl_env", f"{json_type} NOT NULL DEFAULT '{{}}'"),
            ],
        }
        with engine.begin() as conn:
            for table, cols in plan.items():
                have = {c["name"] for c in insp.get_columns(table)}
                for name, decl in cols:
                    if name not in have:
                        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {name} {decl}"))
                        log.info("migration: %s.%s column added", table, name)
    except Exception as e:
        log.warning("migration check failed: %s", e)


# 默认告警规则（新库 seed；阈值型渲染为 Prometheus 规则、事件型由平台故障收敛点触发）
# 与 deploy/monitoring/prometheus/rules/alerts.default.yml 首启兜底规则同口径
DEFAULT_ALERT_RULES = [
    # (name, kind, metric, op, thr, severity, cooldown, event)
    ("GPU 温度过高", "threshold", "gpu_temp", ">", 85, "critical", 300, ""),
    ("GPU 显存过高", "threshold", "gpu_mem", ">", 95, "warning", 300, ""),
    ("GPU XID 错误", "threshold", "gpu_xid", ">", 0, "critical", 300, ""),
    ("GPU ECC 双比特错误", "threshold", "gpu_ecc_dbe", ">", 0, "critical", 300, ""),
    ("宿主 CPU 过高", "threshold", "host_cpu", ">", 90, "warning", 600, ""),
    ("宿主内存过高", "threshold", "host_mem", ">", 90, "warning", 600, ""),
    ("宿主磁盘过高", "threshold", "host_disk", ">", 85, "warning", 900, ""),
    ("磁盘 IO 延时过高", "threshold", "host_disk_io_ms", ">", 200, "warning", 300, ""),
    ("磁盘 IO 利用率过高", "threshold", "host_disk_util", ">", 90, "warning", 300, ""),
    ("推理 KV 缓存过高", "threshold", "kv_cache", ">", 90, "warning", 300, ""),
    ("推理服务离线", "threshold", "infer_down", "<", 1, "critical", 300, ""),
    ("监控目标离线", "threshold", "system_up", "<", 1, "warning", 300, ""),
    ("计算节点离线", "event", "", "", 0, "critical", 300, "node_offline"),
    ("计算节点恢复在线", "event", "", "", 0, "info", 300, "node_online"),
    ("训练任务失败", "event", "", "", 0, "critical", 60, "train_failed"),
    ("量化任务失败", "event", "", "", 0, "warning", 60, "quant_failed"),
    ("评测任务失败", "event", "", "", 0, "warning", 60, "eval_failed"),
    ("推理服务不可用", "event", "", "", 0, "critical", 300, "infer_down"),
    ("登录锁定（疑似暴力破解）", "event", "", "", 0, "warning", 300, "login_locked"),
]


def _seed_category(kind: str, metric: str, event: str) -> str:
    """默认规则的分类（与前端分类预设同词表）；自定义规则由用户填写"""
    if kind == "event":
        return {"train_failed": "task", "eval_failed": "task", "quant_failed": "task",
                "node_offline": "system", "node_online": "system",
                "login_locked": "security", "infer_down": "inference"}.get(event, "other")
    if metric.startswith("gpu_"):
        return "GPU"
    if metric.startswith(("kv_cache", "infer_")):
        return "inference"
    return "system"  # host_* / system_up 等宿主与监控自身指标


def init_db():
    Base.metadata.create_all(bind=engine)
    _drop_legacy_metric_tables()
    _migrate_columns()
    db = SessionLocal()
    try:
        # 超级管理员（必须先建，作为默认项目 owner 的外键目标）
        admin = db.query(User).filter_by(username="admin").first()
        if not admin:
            admin = User(username="admin",
                         password_hash=hash_password(settings.admin_password),
                         full_name="Admin", role="super_admin",  # 中性显示名（多语言 UI 通用）
                         must_change_password=True)  # 默认密码，首登强制修改
            db.add(admin)
            db.flush()
        # 默认租户 + 项目
        if not db.query(Tenant).first():
            t = Tenant(name="default")
            db.add(t)
            db.flush()
            p = Project(tenant_id=t.id, name="default-project",
                        description="默认项目空间", owner_id=admin.id)
            db.add(p)
            db.flush()
            from .models import ProjectMember
            db.add(ProjectMember(project_id=p.id, user_id=admin.id, role="owner"))
        # 本节点
        if not db.query(GpuNode).filter_by(name=settings.node_name).first():
            db.add(GpuNode(name=settings.node_name, gpu_count=0, status="online"))
        # 默认护栏
        if not db.query(GuardRule).first():
            for pat, desc in [
                (r"法轮", "敏感词（默认护栏）"),
                (r"暴恐|恐怖袭击方法", "暴恐内容（默认护栏）"),
            ]:
                db.add(GuardRule(project_id=None, pattern=pat, scope="both", description=desc))
        # 默认告警规则（阈值型渲染为 Prometheus 规则；事件型由平台故障收敛点触发）
        if not db.query(AlertRule).first():
            for name, kind, metric, op, thr, sev, cd, event in DEFAULT_ALERT_RULES:
                db.add(AlertRule(name=name, kind=kind, metric=metric, op=op,
                                 threshold=thr, event=event, severity=sev, cooldown=cd,
                                 category=_seed_category(kind, metric, event)))
        # 存量库幂等补事件规则（新代码新增的事件码，老库没有对应规则则补建）
        for event in ("login_locked",):
            if not db.query(AlertRule).filter(
                    AlertRule.event == event, AlertRule.kind == "event").first():
                db.add(AlertRule(name="登录锁定（疑似暴力破解）", kind="event",
                                 metric="", op="", threshold=0, event=event,
                                 severity="warning", cooldown=300,
                                 category=_seed_category("event", "", event)))
        # 存量库幂等补默认阈值规则（2026-11 新增存储 IO 监控：老库没有对应规则则补建，
        # 用户已自建同指标+操作+阈值规则时不重复补）
        for name, kind, metric, op, thr, sev, cd, event in DEFAULT_ALERT_RULES:
            if kind != "threshold" or metric not in ("host_disk_io_ms", "host_disk_util"):
                continue
            if not db.query(AlertRule).filter(
                    AlertRule.kind == "threshold", AlertRule.metric == metric,
                    AlertRule.op == op, AlertRule.threshold == thr).first():
                db.add(AlertRule(name=name, kind=kind, metric=metric, op=op,
                                 threshold=thr, event=event, severity=sev, cooldown=cd,
                                 category=_seed_category(kind, metric, event)))
        # 存量库幂等补默认规则分类（2026-11 新增分类功能；仅补名称与默认集一致且未分类的行，
        # 用户自定义规则不受影响；已补过的行 category 非空自动跳过）
        _default_cats = {n: _seed_category(k, m, e)
                         for n, k, m, _o, _th, _s, _c, e in DEFAULT_ALERT_RULES}
        for r in db.query(AlertRule).filter(AlertRule.category == "").all():
            if r.name in _default_cats:
                r.category = _default_cats[r.name]
        # 数据目录
        for d in ("models/hub", "datasets", "jobs", "evals", "infer", "gpu",
                  "uploads", "dvc-remote", "hf-cache", "argilla"):
            os.makedirs(os.path.join(settings.data_dir, d), exist_ok=True)
        db.commit()
    finally:
        db.close()


def _s3_bootstrap():
    """后台建桶：rustfs 就绪前反复重试（10s 间隔，最多 10 分钟），成功即退出；
    失败不阻塞平台——S3 写入点均为惰性 ensure_bucket + 失败仅告警（本地缓存仍完整）。"""
    import threading
    import time

    def _loop():
        from .services import storage
        for i in range(60):
            try:
                storage.ensure_bucket()
                log.info("s3 bucket ready: %s @ %s",
                         settings.s3_bucket, settings.s3_endpoint)
                return
            except Exception as e:
                log.info("s3 bootstrap retry %d/60: %s", i + 1, str(e)[:120])
                time.sleep(10)
        log.warning("s3 bootstrap gave up（对象存储暂不可用，写入将仅留本地缓存）")

    threading.Thread(target=_loop, name="s3-bootstrap", daemon=True).start()


def _monitoring_bootstrap():
    """后台把「告警规则/通知通道」DB 配置渲染为 Prometheus/Alertmanager 配置并热加载。
    组件未就绪时反复重试（10s 间隔，最多 10 分钟）；失败不阻塞平台
    （首启由 alerts.default.yml 兜底，规则变更时 API 会再次触发同步）。"""
    import threading
    import time

    def _loop():
        from .services import prom_service
        for i in range(60):
            ok1 = prom_service.sync_rules()
            ok2 = prom_service.sync_channels()
            if ok1 and ok2:
                try:
                    prom_service.resync_infer_sd()  # 重启后按 DB 重建推理 target 文件（file_sd）
                except Exception:
                    log.exception("resync_infer_sd failed")
                log.info("monitoring config synced (rules + alertmanager + inference sd)")
                return
            time.sleep(10)
        log.warning("monitoring bootstrap gave up（规则/通道变更时会在保存时重试）")

    threading.Thread(target=_loop, name="monitoring-bootstrap", daemon=True).start()


def _gateway_rate_bootstrap():
    """后台自愈：DB 已保存网关限流配置时重新渲染并热应用（覆盖保存时 docker
    不可达/容器未就绪造成的漂移）。从未配置则直接返回（用镜像默认值）。"""
    import threading
    import time
    from .models import GatewayRateConfig

    def _loop():
        from .services import gateway_service
        for _ in range(30):  # web 容器可能还没起：10s 间隔，最多 5 分钟
            db = SessionLocal()
            try:
                row = db.get(GatewayRateConfig, 1)
            finally:
                db.close()
            if row is None:
                return
            res = gateway_service.apply_zones(gateway_service.render_zones(
                row.login_rate, row.api_rate, row.v1_rate, row.conn_limit))
            if res["ok"]:
                log.info("gateway rate config reapplied (self-heal): %s", res["detail"])
                return
            time.sleep(10)
        log.warning("gateway rate bootstrap gave up（下次保存时会重试）")

    threading.Thread(target=_loop, name="gateway-rate-bootstrap", daemon=True).start()


@app.on_event("startup")
def startup():
    init_db()
    start_scheduler()
    if settings.s3_endpoint:
        _s3_bootstrap()
    _monitoring_bootstrap()
    _gateway_rate_bootstrap()
    log.info("VerveDeck backend ready. data_dir=%s", settings.data_dir)


@app.get("/health")
def health():
    return {"status": "ok", "service": "vervedeck-backend"}


# ---------- 自监控（/metrics 供 Prometheus backend job 抓取） ----------
from fastapi import Request  # noqa: E402
from fastapi.responses import Response, JSONResponse  # noqa: E402
from prometheus_client import Gauge, generate_latest, CONTENT_TYPE_LATEST  # noqa: E402
from .models import TrainingJob, EvalTask, QuantTask, Deployment  # noqa: E402

_ACTIVE = Gauge("vervedeck_active_tasks", "当前活跃任务数", ["kind"])


@app.get("/metrics", include_in_schema=False)
def metrics():
    """backend 自监控指标（active 计数惰性查询；进程/Python 基础指标由 client 自动提供）"""
    db = SessionLocal()
    try:
        _ACTIVE.labels(kind="training").set(
            db.query(TrainingJob).filter(TrainingJob.status == "running").count())
        _ACTIVE.labels(kind="eval").set(
            db.query(EvalTask).filter(EvalTask.status == "running").count())
        _ACTIVE.labels(kind="quant").set(
            db.query(QuantTask).filter(QuantTask.status == "running").count())
        _ACTIVE.labels(kind="inference").set(
            db.query(Deployment).filter(Deployment.status == "running").count())
    finally:
        db.close()
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.post("/api/internal/am-webhook")
async def am_webhook(req: Request):
    """Alertmanager 回调（仅内部组件经 X-Vervedeck-Token 头访问）：
    企微通知 + 告警事件同步入库；邮件由 Alertmanager 原生发出，不重复。"""
    from .services import prom_service
    if req.headers.get("x-vervedeck-token") != settings.am_webhook_token:
        return JSONResponse({"ok": False, "error": "unauthorized"}, status_code=401)
    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "invalid json"}, status_code=400)
    db = SessionLocal()
    try:
        return prom_service.am_webhook(db, payload or {})
    finally:
        db.close()


@app.get("/api/version")
def version():
    """前端页脚展示的软件版本 / 开源 licence / GitHub 仓库。"""
    return {
        "name": "VerveDeck",
        "version": APP_VERSION,
        "license": APP_LICENSE,
        "license_url": "https://www.apache.org/licenses/LICENSE-2.0",
        "github": APP_GITHUB,
        "service": "vervedeck-backend",
    }


app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(datasets.router)
app.include_router(models_api.router)
app.include_router(training.router)
app.include_router(evals.router)
app.include_router(inference.router)
app.include_router(cluster.router)
app.include_router(dashboard.router)
app.include_router(quantize.router)
app.include_router(storage.router)
app.include_router(logs.router)
app.include_router(images.router)

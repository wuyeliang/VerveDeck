"""VerveDeck 大模型微调训练推理平台 - 后端入口"""
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
                  inference, cluster, dashboard, quantize, storage, logs, images,
                  backends, cache, model_routes)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

APP_VERSION = "1.1"
APP_LICENSE = "Apache-2.0"
APP_GITHUB = "https://github.com/wuyeliang/VerveDeck"

# 安全加固：公网部署可设 API_DOCS_ENABLED=false 关闭接口文档（防接口清单泄露）
_DOCS = settings.api_docs_enabled
app = FastAPI(title="VerveDeck 大模型微调训练推理平台", version=APP_VERSION,
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
                # 存储位置选择：空=平台默认；指向 storage_backends.id（可空，不设约束）
                ("storage_backend_id", "INTEGER"),
            ],
            "deployments": [
                ("description", "TEXT NOT NULL DEFAULT ''"),
                # 推理高级参数（#96）：JSON 兜底，留空=引擎默认，存量部署行为不变
                ("advanced", f"{json_type} NOT NULL DEFAULT '{{}}'"),
                # 部署拓扑：PD 分离 / 多级多卡 / RDMA（#103）；存量部署默认 off=协同，行为不变
                ("pd_mode", "VARCHAR(16) NOT NULL DEFAULT 'off'"),
                ("prefill_replicas", "INTEGER NOT NULL DEFAULT 1"),
                ("decode_replicas", "INTEGER NOT NULL DEFAULT 1"),
                ("prefill_tp", "INTEGER NOT NULL DEFAULT 1"),
                ("decode_tp", "INTEGER NOT NULL DEFAULT 1"),
                ("kv_connector", "VARCHAR(32) NOT NULL DEFAULT 'nixl'"),
                ("node_count", "INTEGER NOT NULL DEFAULT 1"),
                ("nccl_env", f"{json_type} NOT NULL DEFAULT '{{}}'"),
                # 推理重构（网关/后端注册表/扩展 KV/实例运维）：默认值保持存量部署行为不变
                ("state_message", "VARCHAR(512) NOT NULL DEFAULT ''"),
                ("restart_on_error", "BOOLEAN NOT NULL DEFAULT 'true'"),
                ("error_count", "INTEGER NOT NULL DEFAULT 0"),
                ("next_restart_at", "TIMESTAMP"),
                ("kv_cache_ext", f"{json_type} NOT NULL DEFAULT '{{}}'"),
                ("gateway_port", "INTEGER NOT NULL DEFAULT 0"),
                ("backend_name", "VARCHAR(64)"),
                ("backend_version", "VARCHAR(64)"),
                ("cache_service_id", "INTEGER"),
                # 部署放置（GPUWorks 同构：目标节点/标签约束/GPU 选择）+ 镜像/容器运行时/命令覆盖
                # 默认值全部保持存量部署行为不变（自动选择/自动分卡/系统镜像/自动拼装命令）
                ("node_name", "VARCHAR(64)"),
                ("node_labels", f"{json_type} NOT NULL DEFAULT '[]'"),
                ("gpu_ids", f"{json_type} NOT NULL DEFAULT '[]'"),
                ("pipeline_parallel", "INTEGER NOT NULL DEFAULT 1"),
                ("image_ref", "VARCHAR(255) NOT NULL DEFAULT ''"),
                ("docker_args", f"{json_type} NOT NULL DEFAULT '[]'"),
                ("cmd_override", "TEXT NOT NULL DEFAULT ''"),
                ("engine_version", "VARCHAR(64) NOT NULL DEFAULT ''"),
            ],
            "users": [("must_change_password", "BOOLEAN NOT NULL DEFAULT 'false'")],
            "model_assets": [
                ("category", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("tags", f"{json_type} NOT NULL DEFAULT '[]'"),
                # 存储位置选择：空=平台默认；指向 storage_backends.id（可空，不设约束）
                ("storage_backend_id", "INTEGER"),
            ],
            "audit_logs": [("action", "VARCHAR(64) NOT NULL DEFAULT ''")],
            # 节点状态机（#105 添加节点重构）：状态说明 + 维护模式
            "gpu_nodes": [
                ("state_message", "VARCHAR(512) NOT NULL DEFAULT ''"),
                ("maintenance", "BOOLEAN NOT NULL DEFAULT 'false'"),
                ("maintenance_message", "VARCHAR(256) NOT NULL DEFAULT ''"),
                # 节点标签（"key=value" 列表）：部署「节点标签约束」子集匹配的数据源
                ("labels", f"{json_type} NOT NULL DEFAULT '[]'"),
            ],
            "api_keys": [("model_ids", f"{json_type} NOT NULL DEFAULT '[]'")],
            # 模型路由增强（自动关联/路由策略/路由级缓存）：默认值保持存量路由行为不变（lb 权重+容灾）
            "model_routes": [
                ("policy", "VARCHAR(16) NOT NULL DEFAULT 'lb'"),
                ("cache_service_id", "INTEGER"),
                ("auto", "BOOLEAN NOT NULL DEFAULT 'false'"),
            ],
            "model_route_targets": [
                ("priority", "INTEGER NOT NULL DEFAULT 1"),
            ],
            # 缓存加速页（类型内存|SSD + 容量）：存量默认 memory/0，行为不变
            "cache_services": [
                ("cache_type", "VARCHAR(16) NOT NULL DEFAULT 'memory'"),
                ("capacity_gb", "INTEGER NOT NULL DEFAULT 0"),
            ],
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
            # 存量列类型升级：size_bytes 32 位整型在 >2GB 模型/数据集产物上溢出
            # （2026-11-18 全参检查点 2.9GB 注册即 integer out of range 实测）。
            # 仅 postgres 需要（sqlite 的 INTEGER 本身任意精度）
            if engine.dialect.name != "sqlite":
                for table, col in (("model_assets", "size_bytes"),
                                   ("dataset_versions", "size_bytes")):
                    tnames = {c["name"]: str(c["type"]).upper()
                              for c in insp.get_columns(table)}
                    if tnames.get(col, "").startswith("INT") and \
                            not tnames.get(col, "").startswith("BIGINT"):
                        conn.execute(text(
                            f"ALTER TABLE {table} ALTER COLUMN {col} TYPE BIGINT"))
                        log.info("migration: %s.%s widened to BIGINT", table, col)
    except Exception as e:
        log.warning("migration check failed: %s", e)


# 默认告警规则（新库 seed；阈值型渲染为 Prometheus 规则、事件型由平台故障收敛点触发）
# 与 deploy/monitoring/prometheus/rules/alerts.default.yml 首启兜底规则同口径
# ---------- 默认告警规则集 ----------
# 设计：「核心启用，对齐行业水位」（业界告警规则即数十条核心告警，数百条会造成告警疲劳）
#   - 39 条核心规则：全覆盖 21 个可告警指标，合理阈值，默认启用
#   - 两档阈值：warning + critical（GPU/宿主/推理/系统四大域）+ 事件型告警（节点/任务/登录）
#   - 不预置批量禁用阈值模板；需要更多阈值档时由用户在界面按需搜索/启用或批量导入
#   - reconcile 幂等补建（存量库保留用户自定义、不改已有），事件型重复项自动清理

# 核心规则（默认启用）
# (name, kind, metric, op, thr, severity, cooldown, event)
DEFAULT_ALERT_RULES = [
    # GPU
    ("GPU 温度过高", "threshold", "gpu_temp", ">", 85, "critical", 300, ""),
    ("GPU 温度严重", "threshold", "gpu_temp", ">", 90, "critical", 300, ""),
    ("GPU 显存过高", "threshold", "gpu_mem", ">", 95, "warning", 300, ""),
    ("GPU 显存严重", "threshold", "gpu_mem", ">", 98, "critical", 300, ""),
    ("GPU 显存温度过高", "threshold", "gpu_mem_temp", ">", 95, "warning", 300, ""),
    ("GPU 显存温度严重", "threshold", "gpu_mem_temp", ">", 100, "critical", 300, ""),
    ("GPU 功耗接近限额", "threshold", "gpu_power", ">", 450, "warning", 600, ""),
    ("GPU 持续空闲（低利用率）", "threshold", "gpu_util", "<", 5, "warning", 1800, ""),
    ("GPU XID 错误", "threshold", "gpu_xid", ">", 0, "critical", 300, ""),
    ("GPU ECC 双比特错误", "threshold", "gpu_ecc_dbe", ">", 0, "critical", 300, ""),
    # 宿主
    ("宿主 CPU 过高", "threshold", "host_cpu", ">", 90, "warning", 600, ""),
    ("宿主 CPU 严重", "threshold", "host_cpu", ">", 95, "critical", 600, ""),
    ("宿主内存过高", "threshold", "host_mem", ">", 90, "warning", 600, ""),
    ("宿主内存严重", "threshold", "host_mem", ">", 95, "critical", 600, ""),
    ("宿主磁盘过高", "threshold", "host_disk", ">", 85, "warning", 900, ""),
    ("宿主磁盘严重", "threshold", "host_disk", ">", 90, "critical", 900, ""),
    ("磁盘 IO 延时过高", "threshold", "host_disk_io_ms", ">", 200, "warning", 300, ""),
    ("磁盘 IO 延时严重", "threshold", "host_disk_io_ms", ">", 400, "critical", 300, ""),
    ("磁盘 IO 利用率过高", "threshold", "host_disk_util", ">", 90, "warning", 300, ""),
    ("磁盘 IO 利用率严重", "threshold", "host_disk_util", ">", 95, "critical", 300, ""),
    ("网络接收带宽过高", "threshold", "host_net_rx", ">", 1000000000, "warning", 600, ""),
    ("网络发送带宽过高", "threshold", "host_net_tx", ">", 1000000000, "warning", 600, ""),
    ("磁盘读吞吐过高", "threshold", "host_disk_read", ">", 500000000, "warning", 600, ""),
    ("磁盘写吞吐过高", "threshold", "host_disk_write", ">", 500000000, "warning", 600, ""),
    # 推理 / 系统
    ("推理 KV 缓存过高", "threshold", "kv_cache", ">", 90, "warning", 300, ""),
    ("推理 KV 缓存严重", "threshold", "kv_cache", ">", 95, "critical", 300, ""),
    ("推理排队过长", "threshold", "infer_waiting", ">", 20, "warning", 300, ""),
    ("推理排队拥塞", "threshold", "infer_waiting", ">", 50, "critical", 300, ""),
    ("推理延迟 P95 过高", "threshold", "infer_latency_p95", ">", 3, "warning", 300, ""),
    ("推理延迟 P95 严重", "threshold", "infer_latency_p95", ">", 8, "critical", 300, ""),
    ("推理服务离线", "threshold", "infer_down", "<", 1, "critical", 300, ""),
    ("监控目标离线", "threshold", "system_up", "<", 1, "warning", 300, ""),
    # 事件
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


def _reconcile_alert_rules(db) -> int:
    """幂等对齐默认规则：补建缺失项（按 唯一键 判断 DB 无则新建），
    保留用户自定义（不改/不删已有），清理事件型重复项（历史重复种子）。
    对齐行业水平：核心规则数十条（21 指标全覆盖 + warning/critical 两档阈值），
    不预置批量禁用阈值模板（避免告警疲劳）；需更多阈值时由用户在界面按需启用/新建
    （容量 5000 + 分类 + 批量导入）。每次启动 init_db 调用；返回新建条数。"""
    created = 0
    existing = {(r.kind, r.metric or "", r.op or "", r.threshold, r.event or "")
                for r in db.query(AlertRule).all()}
    for name, kind, metric, op, thr, sev, cd, event in DEFAULT_ALERT_RULES:
        key = (kind, metric, op, thr, event)
        if key in existing:
            continue
        db.add(AlertRule(name=name, kind=kind, metric=metric, op=op,
                         threshold=thr, event=event, severity=sev, cooldown=cd,
                         enabled=True, category=_seed_category(kind, metric, event)))
        existing.add(key)
        created += 1
    # 事件型重复清理（同 event+kind 保留最小 id）
    by_event: dict[str, list] = {}
    for r in db.query(AlertRule).filter(
            AlertRule.kind == "event", AlertRule.event != "").all():
        by_event.setdefault(r.event, []).append(r)
    for rows in by_event.values():
        if len(rows) <= 1:
            continue
        rows.sort(key=lambda x: x.id)
        for dup in rows[1:]:
            db.delete(dup)
    return created


def _backfill_auto_routes():
    """存量部署自动补同名路由（部署创建自动关联路由的存量对齐）。

    幂等：部署无同名路由 → 建 auto 路由（单目标=该部署）；有同名路由但
    未挂该部署 → 补目标。存量库在互斥约束下不存在同名路由，均为新建。"""
    from .api.model_routes import ensure_auto_route
    db = SessionLocal()
    try:
        changed = 0
        for d in db.query(Deployment).all():
            if ensure_auto_route(db, d, d.created_by):
                changed += 1
        if changed:
            db.commit()
            log.info("auto route backfill: %d", changed)
    except Exception:
        db.rollback()
        log.exception("auto route backfill failed")
    finally:
        db.close()


def init_db():
    Base.metadata.create_all(bind=engine)
    _drop_legacy_metric_tables()
    _migrate_columns()
    _backfill_auto_routes()
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
        # 默认告警规则：39 条核心（对齐行业水位），幂等补建
        # 新库建全套 39 核心；存量库仅补缺失规则（保留用户自定义、不改已有）；
        # 事件型重复项自动清理（历史重复种子）。阈值型渲染为 Prometheus 规则，事件型由故障收敛点触发
        _created = _reconcile_alert_rules(db)
        # 存量库幂等补默认规则分类（仅补 category 为空且名称与默认集一致的行；
        # 用户自定义规则不受影响；已补过的行 category 非空自动跳过）
        _default_cats = {n: _seed_category(k, m, e)
                         for n, k, m, _o, _th, _s, _c, e in DEFAULT_ALERT_RULES}
        for r in db.query(AlertRule).filter(AlertRule.category == "").all():
            if r.name in _default_cats:
                r.category = _default_cats[r.name]
        if _created:
            log.info("默认告警规则对齐（行业 39 核心）：新建 %d 条", _created)
        # 推理后端注册表：builtin（vllm/sglang）种子幂等补建（基线版本+参数默认值）
        from .services import backend_registry
        _n_backends = backend_registry.seed_builtin_backends(db)
        if _n_backends:
            log.info("推理后端注册表种子：新建 %d 条", _n_backends)
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
app.include_router(backends.router)
app.include_router(cache.router)
app.include_router(model_routes.router)

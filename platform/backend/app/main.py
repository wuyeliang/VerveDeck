"""VerveDeck 大模型微调训练平台 - 后端入口"""
import logging
import os

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .config import settings
from .database import Base, engine, SessionLocal
from . import models  # noqa: F401  注册 ORM
from .security import hash_password
from .models import User, Tenant, Project, GpuNode, GuardRule
from .audit import AuditMiddleware
from .scheduler import start_scheduler
from .api import (auth, admin, datasets, models_api, training, evals,
                  inference, cluster, dashboard, quantize)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("main")

APP_VERSION = "1.1"
APP_LICENSE = "Apache-2.0"
APP_GITHUB = "https://github.com/wuyeliang/VerveDeck"

app = FastAPI(title="VerveDeck 大模型微调训练平台", version=APP_VERSION)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"])
app.add_middleware(AuditMiddleware)


def _migrate_columns():
    """旧库补列（create_all 只建缺失表、不补已有表的列）。幂等。"""
    from sqlalchemy import inspect, text
    try:
        insp = inspect(engine)
        json_type = "JSON" if engine.dialect.name == "sqlite" else "JSONB"
        plan = {
            "gpu_metrics": [("extra", f"{json_type} NOT NULL DEFAULT '{{}}'")],
            "datasets": [
                ("kind", "VARCHAR(16) NOT NULL DEFAULT 'train'"),
                ("scenario", "VARCHAR(16) NOT NULL DEFAULT 'text'"),
            ],
            "deployments": [("description", "TEXT NOT NULL DEFAULT ''")],
            "users": [("must_change_password", "BOOLEAN NOT NULL DEFAULT 'false'")],
            "model_assets": [
                ("category", "VARCHAR(64) NOT NULL DEFAULT ''"),
                ("tags", f"{json_type} NOT NULL DEFAULT '[]'"),
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


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate_columns()
    db = SessionLocal()
    try:
        # 超级管理员（必须先建，作为默认项目 owner 的外键目标）
        admin = db.query(User).filter_by(username="admin").first()
        if not admin:
            admin = User(username="admin",
                         password_hash=hash_password(settings.admin_password),
                         full_name="平台管理员", role="super_admin",
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


@app.on_event("startup")
def startup():
    init_db()
    start_scheduler()
    if settings.s3_endpoint:
        _s3_bootstrap()
    log.info("VerveDeck backend ready. data_dir=%s", settings.data_dir)


@app.get("/health")
def health():
    return {"status": "ok", "service": "vervedeck-backend"}


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

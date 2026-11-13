from datetime import datetime
from sqlalchemy import (String, Integer, Float, Boolean, DateTime, ForeignKey,
                        Text, JSON, Index)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base


def now():
    return datetime.utcnow()


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    full_name: Mapped[str] = mapped_column(String(128), default="")
    role: Mapped[str] = mapped_column(String(32), default="developer")  # super_admin/tenant_admin/developer/viewer
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Project(Base):
    __tablename__ = "projects"
    id: Mapped[int] = mapped_column(primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    owner_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (Index("ix_proj_tenant_name", "tenant_id", "name", unique=True),)


class ProjectMember(Base):
    __tablename__ = "project_members"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(32), default="developer")  # owner/developer/viewer
    __table_args__ = (Index("ix_pm_pair", "project_id", "user_id", unique=True),)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    key_hash: Mapped[str] = mapped_column(String(255))
    key_prefix: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (Index("ix_ds_proj_name", "project_id", "name", unique=True),)


class DatasetVersion(Base):
    __tablename__ = "dataset_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    dvc_rev: Mapped[str] = mapped_column(String(64), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    fmt: Mapped[str] = mapped_column(String(32), default="alpaca")  # alpaca/sharegpt/dpo
    status: Mapped[str] = mapped_column(String(32), default="draft")  # draft/committed
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (Index("ix_dv_ds_ver", "dataset_id", "version", unique=True),)


class DataTask(Base):
    """解析/清洗/合成 等数据任务（统一表）"""
    __tablename__ = "data_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))  # parse/clean/synth
    dataset_id: Mapped[int] = mapped_column(ForeignKey("datasets.id"), index=True)
    source_version_id: Mapped[int | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    output_version_id: Mapped[int | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    file_name: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/running/done/failed
    progress: Mapped[int] = mapped_column(Integer, default=0)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class ModelAsset(Base):
    __tablename__ = "model_assets"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    type: Mapped[str] = mapped_column(String(32))  # base/lora/merged
    path: Mapped[str] = mapped_column(String(512), default="")
    source: Mapped[str] = mapped_column(String(512), default="")  # hf:org/repo 或 本地路径
    hf_id: Mapped[str] = mapped_column(String(255), default="")
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    parent_asset_id: Mapped[int | None] = mapped_column(ForeignKey("model_assets.id"), nullable=True)  # 血缘: 基于哪个模型
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 血缘: 来自哪个训练任务
    dataset_version_ids: Mapped[list] = mapped_column(JSON, default=list)  # 血缘: 用了哪些数据
    status: Mapped[str] = mapped_column(String(32), default="ready")  # pending_download/ready/failed
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    __table_args__ = (Index("ix_ma_proj_name", "project_id", "name", unique=True),)


class TrainingJob(Base):
    __tablename__ = "training_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    stage: Mapped[str] = mapped_column(String(32), default="sft")  # pretrain(CPT)/sft/dpo/export(合并)
    finetuning_type: Mapped[str] = mapped_column(String(32), default="lora")  # lora/full
    base_model_id: Mapped[int | None] = mapped_column(ForeignKey("model_assets.id"), nullable=True)
    adapter_asset_id: Mapped[int | None] = mapped_column(ForeignKey("model_assets.id"), nullable=True)  # export 用
    dataset_version_ids: Mapped[list] = mapped_column(JSON, default=list)
    hyperparams: Mapped[dict] = mapped_column(JSON, default=dict)
    deepspeed: Mapped[str] = mapped_column(String(16), default="none")  # none/zero2/zero3
    gpu_count: Mapped[int] = mapped_column(Integer, default=1)
    priority: Mapped[int] = mapped_column(Integer, default=5)  # 1-9, 越大越优先
    status: Mapped[str] = mapped_column(String(32), default="queued")  # queued/running/succeeded/failed/cancelled
    container_id: Mapped[str] = mapped_column(String(64), default="")
    container_name: Mapped[str] = mapped_column(String(128), default="")
    work_dir: Mapped[str] = mapped_column(String(512), default="")
    config_path: Mapped[str] = mapped_column(String(512), default="")
    output_dir: Mapped[str] = mapped_column(String(512), default="")
    tensorboard_dir: Mapped[str] = mapped_column(String(512), default="")
    resume_from_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    submitted_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class EvalTask(Base):
    __tablename__ = "eval_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    target_deployment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_asset_id: Mapped[int | None] = mapped_column(ForeignKey("model_assets.id"), nullable=True)
    dataset_version_id: Mapped[int | None] = mapped_column(ForeignKey("dataset_versions.id"), nullable=True)
    benchmark: Mapped[str] = mapped_column(String(64), default="")  # gsm8k/ceval/arc/""
    method: Mapped[str] = mapped_column(String(32), default="rule")  # rule/judge/both/benchmark
    judge_deployment_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    judge_model: Mapped[str] = mapped_column(String(128), default="")
    limit: Mapped[int] = mapped_column(Integer, default=100)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    container_id: Mapped[str] = mapped_column(String(64), default="")
    work_dir: Mapped[str] = mapped_column(String(512), default="")
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class Deployment(Base):
    __tablename__ = "deployments"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    model_asset_id: Mapped[int] = mapped_column(ForeignKey("model_assets.id"))
    engine: Mapped[str] = mapped_column(String(16), default="vllm")  # vllm/sglang
    quantization: Mapped[str] = mapped_column(String(16), default="none")  # none/awq/gptq/int8/fp8
    tensor_parallel: Mapped[int] = mapped_column(Integer, default=1)
    max_model_len: Mapped[int] = mapped_column(Integer, default=8192)
    gpu_mem_util: Mapped[float] = mapped_column(Float, default=0.85)
    enable_lora: Mapped[bool] = mapped_column(Boolean, default=False)
    lora_adapters: Mapped[list] = mapped_column(JSON, default=list)  # [{name, path}]
    port: Mapped[int] = mapped_column(Integer, default=0)  # 容器内端口(固定8000), host 端口池位
    endpoint: Mapped[str] = mapped_column(String(128), default="")  # ip:port
    replicas: Mapped[int] = mapped_column(Integer, default=1)
    container_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(32), default="creating")  # creating/running/stopped/failed
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class GpuNode(Base):
    __tablename__ = "gpu_nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    gpu_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="online")  # online/offline
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class GpuMetric(Base):
    __tablename__ = "gpu_metrics"
    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(ForeignKey("gpu_nodes.id"), index=True)
    ts: Mapped[int] = mapped_column(Integer, index=True)  # unix ts
    gpu_index: Mapped[int] = mapped_column(Integer, default=0)
    mem_used: Mapped[float] = mapped_column(Float, default=0)
    mem_total: Mapped[float] = mapped_column(Float, default=0)
    util: Mapped[float] = mapped_column(Float, default=0)
    power: Mapped[float] = mapped_column(Float, default=0)
    temp: Mapped[float] = mapped_column(Float, default=0)


class GuardRule(Base):
    __tablename__ = "guard_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int | None] = mapped_column(ForeignKey("projects.id"), nullable=True)  # null=全局
    pattern: Mapped[str] = mapped_column(String(512))
    scope: Mapped[str] = mapped_column(String(16), default="both")  # input/output/both
    description: Mapped[str] = mapped_column(String(255), default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[int] = mapped_column(Integer, index=True)
    user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(64), default="")
    method: Mapped[str] = mapped_column(String(8), default="")
    path: Mapped[str] = mapped_column(String(512), default="")
    ip: Mapped[str] = mapped_column(String(64), default="")
    status_code: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(String(512), default="")

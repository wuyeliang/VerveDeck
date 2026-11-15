from datetime import datetime
from sqlalchemy import (String, Integer, BigInteger, Float, Boolean, DateTime, ForeignKey,
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
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=False)  # 首登/被重置后强制改密
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
    model_ids: Mapped[list] = mapped_column(JSON, default=list)  # 限定模型 ID（空 = 项目内全部模型）
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Dataset(Base):
    __tablename__ = "datasets"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="train")  # train/eval（训练集/评测集）
    scenario: Mapped[str] = mapped_column(String(16), default="text")  # text/vlm（文本生成/多模态）
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
    category: Mapped[str] = mapped_column(String(64), default="")  # 业务分类（对话/代码/数学/...），空=未分类
    tags: Mapped[list] = mapped_column(JSON, default=list)  # 自由标签
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
    description: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class QuantTask(Base):
    """模型压缩（量化）任务：llamafactory-cli export 产出可部署量化模型"""
    __tablename__ = "quant_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    project_id: Mapped[int] = mapped_column(ForeignKey("projects.id"), index=True)
    name: Mapped[str] = mapped_column(String(160))
    model_asset_id: Mapped[int] = mapped_column(ForeignKey("model_assets.id"))
    method: Mapped[str] = mapped_column(String(16), default="bnb4")  # bnb4/bnb8/gptq4/awq4
    gpu_count: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="pending")  # pending/running/succeeded/failed
    container_id: Mapped[str] = mapped_column(String(64), default="")
    container_name: Mapped[str] = mapped_column(String(128), default="")
    work_dir: Mapped[str] = mapped_column(String(512), default="")
    output_dir: Mapped[str] = mapped_column(String(512), default="")
    error: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class GpuNode(Base):
    __tablename__ = "gpu_nodes"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    gpu_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="online")  # online/offline
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AlertRule(Base):
    """告警规则：threshold=指标阈值（metric+op+threshold），event=平台事件（event 码）"""
    __tablename__ = "alert_rules"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    kind: Mapped[str] = mapped_column(String(16), default="threshold")  # threshold/event
    metric: Mapped[str] = mapped_column(String(32), default="")  # threshold: host_cpu/host_mem/host_disk/gpu_util/gpu_temp/gpu_mem/gpu_mem_temp/gpu_power/gpu_xid/gpu_ecc_dbe/kv_cache/infer_waiting/infer_latency_p95/infer_down/system_up
    op: Mapped[str] = mapped_column(String(4), default=">")  # > < >= <=
    threshold: Mapped[float] = mapped_column(Float, default=0)
    event: Mapped[str] = mapped_column(String(32), default="")  # event: node_offline/node_online/train_failed/eval_failed/quant_failed/infer_down
    severity: Mapped[str] = mapped_column(String(16), default="warning")  # warning/critical
    cooldown: Mapped[int] = mapped_column(Integer, default=300)  # 同目标重复告警冷却（秒）
    channels: Mapped[list] = mapped_column(JSON, default=list)  # ["wecom","email"]，空=全部已配置通道
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AlertEvent(Base):
    """告警事件（触发即记录；notified=是否成功发出通知）"""
    __tablename__ = "alert_events"
    id: Mapped[int] = mapped_column(primary_key=True)
    ts: Mapped[int] = mapped_column(Integer, index=True)  # unix ts
    rule_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    level: Mapped[str] = mapped_column(String(16), default="warning")  # warning/critical/info
    target: Mapped[str] = mapped_column(String(128), default="")  # node-1 / gpu:0 / train:12 / deploy:3
    title: Mapped[str] = mapped_column(String(255), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AlertChannelConfig(Base):
    """告警通知通道配置（单行表 id=1）：企业微信群机器人 + SMTP 邮件。
    UI 可配置（系统管理→通知通道），优先于环境变量；未配置的通道回退 env。"""
    __tablename__ = "alert_channel_config"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    wecom_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    wecom_webhook: Mapped[str] = mapped_column(String(512), default="")
    smtp_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    smtp_host: Mapped[str] = mapped_column(String(128), default="")
    smtp_port: Mapped[int] = mapped_column(Integer, default=465)
    smtp_ssl: Mapped[bool] = mapped_column(Boolean, default=True)  # True=SSL(465)，False=STARTTLS(587)
    smtp_user: Mapped[str] = mapped_column(String(128), default="")
    smtp_password: Mapped[str] = mapped_column(String(256), default="")
    smtp_from: Mapped[str] = mapped_column(String(128), default="")
    smtp_to: Mapped[str] = mapped_column(String(512), default="")  # 收件人，逗号分隔
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class LoginSecurityConfig(Base):
    """登录安全配置（单行表 id=1）：IP 白名单 + 防暴破锁定参数。
    UI 可配置（系统管理→设置）；未配置时用 auth 内置默认（5 次/30 次/15 分钟）。"""
    __tablename__ = "login_security_config"
    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    ip_whitelist_enabled: Mapped[bool] = mapped_column(Boolean, default=False)  # 关闭=不限制
    ip_whitelist: Mapped[str] = mapped_column(Text, default="")  # 逗号/换行分隔，支持 CIDR（1.2.3.4/24）
    user_attempts: Mapped[int] = mapped_column(Integer, default=5)   # 单账号失败次数阈值
    ip_attempts: Mapped[int] = mapped_column(Integer, default=30)    # 单 IP 失败次数阈值
    lock_minutes: Mapped[int] = mapped_column(Integer, default=15)   # 锁定时长（分钟）
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


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
    action: Mapped[str] = mapped_column(String(64), default="", index=True)  # 动作码（i18n 展示 + 筛选）


class CustomImage(Base):
    """自定义镜像登记（国产芯片 vllm/sglang/llamafactory 等自托管镜像）"""
    __tablename__ = "custom_images"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)  # 完整镜像引用 name:tag
    note: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # present/missing/unknown
    size: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class StorageBackend(Base):
    """存储后端注册表：对象存储/本地/NFS/块存储等多类型对接"""
    __tablename__ = "storage_backends"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    type: Mapped[str] = mapped_column(String(16), default="object")  # object/local/nfs/block
    # config 按类型：object={endpoint,access_key,secret_key,bucket}
    #              local/nfs/block={path, [server,export|device]}
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="unknown")  # ok/error/unknown
    detail: Mapped[str] = mapped_column(String(512), default="")
    size_used: Mapped[int] = mapped_column(BigInteger, default=0)
    size_total: Mapped[int] = mapped_column(BigInteger, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class UpgradeTask(Base):
    """集群升级任务：目标组件 / 版本变迁 / 状态 / 日志（后台线程执行，幂等可重试）"""
    __tablename__ = "upgrade_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    target: Mapped[str] = mapped_column(String(32))  # vllm/sglang/llamafactory/gpu_agent/backend/web
    from_version: Mapped[str] = mapped_column(String(160), default="")
    to_version: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/success/failed
    log: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

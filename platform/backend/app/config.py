from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_url: str = "postgresql://fintune:fintune123@postgres:5432/fintune"
    data_dir: str = "/data"
    docker_network: str = "fintune_net"
    jwt_secret: str = "vervedeck-dev-secret-change-me"
    jwt_expire_hours: int = 72
    admin_password: str = "admin123"
    hf_endpoint: str = "https://hf-mirror.com"
    mineru_url: str = "http://mineru:8000"
    argilla_url: str = "http://argilla:3000"
    argilla_api_key: str = "fintune-argilla-key"
    infer_port_base: int = 8100
    # 推理引擎优先用官方开源镜像（CUDA 随镜像内置，不装宿主机）：
    # vllm/vllm-openai = CUDA 13.5 + vLLM 0.33.0；lmsysorg/sglang = CUDA 13.0 + SGLang 0.5.23
    # 训练引擎用 LLaMA-Factory 官方预构建镜像（CUDA 12.4 + torch，免本地编译；
    # 离线/气隔环境可用 images/llama-factory 的自构 Dockerfile 兜底）
    images_train: str = "hiyouga/llamafactory:0.9.5"
    images_vllm: str = "vllm/vllm-openai:v0.33.0"
    images_sglang: str = "lmsysorg/sglang:v0.5.23"
    images_eval: str = "fintune/evalscope:1.0"
    node_name: str = "node-1"
    # 对象存储（rustfs / S3 兼容）：数据集版本文件主存储，本地 /data 为处理缓存
    s3_endpoint: str = ""  # 如 http://rustfs:9000；为空则纯本地存储
    s3_access_key: str = "rustfsadmin"
    s3_secret_key: str = "rustfsadmin"
    s3_bucket: str = "vervedeck-datasets"
    # 告警通知通道（留空 = 不启用该通道；规则与收件配置见 系统管理→告警）
    wecom_webhook: str = ""  # 企业微信群机器人 webhook（https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...）
    smtp_host: str = ""      # 如 smtp.exmail.qq.com
    smtp_port: int = 465
    smtp_user: str = ""
    smtp_password: str = ""  # 邮箱授权码（非登录密码）
    smtp_from: str = ""      # 发件人地址（默认同 smtp_user）
    alert_email_to: str = ""  # 告警收件人，逗号分隔
    # 监控告警栈：指标底座 Prometheus，规则/通知配置由 backend 渲染到 /data/monitoring 并热加载
    prometheus_url: str = "http://prometheus:9090"
    alertmanager_url: str = "http://alertmanager:9093"
    backend_internal_url: str = "http://backend:3001"  # Alertmanager webhook 回调地址
    am_webhook_token: str = "vervedeck-am-internal"  # /api/internal/am-webhook 鉴权头（防公网伪造回调）

    class Config:
        env_file = ".env"


settings = Settings()

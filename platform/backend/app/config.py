from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    db_url: str = "postgresql://fintune:fintune123@postgres:5432/fintune"
    data_dir: str = "/data"
    docker_network: str = "fintune_net"
    jwt_secret: str = "vervedeck-dev-secret-change-me"
    jwt_expire_hours: int = 72
    admin_password: str = "admin123"
    # 安全加固（公网部署建议收紧）：
    # CORS 允许来源，逗号分隔；默认 * （前端同源部署经 nginx 转发，实际不跨域）
    cors_origins: str = "*"
    # 公网部署建议 false：关闭 /docs、/redoc、/openapi.json（防接口清单泄露）
    api_docs_enabled: bool = True
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
    # 评测引擎用官方开源 ModelScope 镜像（内置 evalscope/openai/jieba/rouge-chinese，
    # 免本地编译；平台 runner.py 由 backend 运行时投递，见 eval_service）。
    # 离线/气隔环境可覆盖为 fintune/evalscope:1.0（/repo 自构兜底，images/evalscope）
    images_eval: str = ("modelscope-registry.cn-hangzhou.cr.aliyuncs.com/modelscope-repo/"
                        "modelscope:ubuntu22.04-cuda12.4.0-py311-torch2.6.0-1.26.0-LLM")
    node_name: str = "node-1"
    # 推理网关（Higress All-in-One）：数据面承载模型路由的加权/容灾/重试，
    # 控制面经 Console API 同步（Basic Auth）。Console 端口 8001（网关 HTTP 8080），
    # 仅 docker 网络内可达（compose 不映射 8001 到宿主公网）。
    higress_enabled: bool = True
    higress_console_url: str = "http://higress:8001"
    higress_console_username: str = "admin"
    higress_console_password: str = "admin"
    # 路由上游解析目标（docker 服务名，Envoy 内 DNS 解析）：backend 主端口 + 端口池。
    # 网关服务源域名取 <服务名>.local（Console 域名校验限制），compose 须为该服务
    # 注册同名网络别名（backend: aliases [backend.local]）
    higress_upstream_host: str = "backend"
    # 内部目标代理端口池（每部署一个端口，承载认证+护栏+PD 转发的完整代理）
    gateway_port_min: int = 3100
    gateway_port_max: int = 3199
    # 网关经路由 headerControl 向目标代理注入的内部令牌（防网络内旁路直调 31xx）
    target_proxy_token: str = "vervedeck-target-proxy-internal"
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

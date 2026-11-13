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
    images_train: str = "fintune/llama-factory:1.0"
    images_vllm: str = "fintune/vllm:1.0"
    images_sglang: str = "fintune/sglang:1.0"
    images_eval: str = "fintune/evalscope:1.0"
    node_name: str = "node-1"
    # 对象存储（rustfs / S3 兼容）：数据集版本文件主存储，本地 /data 为处理缓存
    s3_endpoint: str = ""  # 如 http://rustfs:9000；为空则纯本地存储
    s3_access_key: str = "rustfsadmin"
    s3_secret_key: str = "rustfsadmin"
    s3_bucket: str = "vervedeck-datasets"

    class Config:
        env_file = ".env"


settings = Settings()

FROM python:3.12-slim

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/data/hf-cache

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
# SGLang 0.5.23（2026-11 最新稳定版）：Python>=3.11、torch==2.14.1
# （默认 wheel 为 CUDA 13.0 构建）、transformers==5.19.0 均由其硬依赖带入。
RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir "sglang==0.5.23" "huggingface_hub"

CMD ["sleep", "infinity"]

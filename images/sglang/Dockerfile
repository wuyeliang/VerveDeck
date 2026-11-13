FROM python:3.10-slim

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/data/hf-cache

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir "sglang" "huggingface_hub"

CMD ["sleep", "infinity"]

FROM python:3.10-slim

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/data/hf-cache

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir "evalscope" "openai" "jieba" "rouge-chinese" \
    && pip install --no-cache-dir "torch" \
    && pip install --no-cache-dir "transformers>=4.49,<5"

# 内置轻量评测运行器：规则匹配 / LLM-as-a-Judge / 自定义测试集
COPY runner.py /workspace/runner.py

CMD ["sleep", "infinity"]

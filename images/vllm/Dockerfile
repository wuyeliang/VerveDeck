FROM python:3.12-slim

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/data/hf-cache \
    VLLM_LOGGING_LEVEL=warning

# gcc/g++ 供 torch._inductor 编译 CUDA kernel（vLLM V1 引擎必需）
RUN apt-get update && apt-get install -y --no-install-recommends curl gcc g++ && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
# vLLM 0.33.0（2026-11 最新稳定版）：Python>=3.11、硬依赖 torch==2.14.0
# （PyPI 默认 wheel 为 CUDA 13.0.3 构建，驱动需 ≥595/推荐 620）、
# transformers>=5.16.1,<5.21。旧 0.15.1 的 LoRA 热加载 API 依赖已不再需要
# （后端仅用标准 OpenAI 端点 + 启动参数）。
RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir "vllm==0.33.0"
# transformers 单独一层：版本调整时无需重装 vllm/torch（构建缓存复用）
RUN pip install --no-cache-dir "huggingface_hub" "transformers>=5.16,<5.21"

CMD ["sleep", "infinity"]

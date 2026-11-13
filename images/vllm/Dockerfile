FROM python:3.10-slim

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/data/hf-cache \
    VLLM_LOGGING_LEVEL=warning

# gcc/g++ 供 torch._inductor 编译 CUDA kernel（vLLM V1 引擎必需）
RUN apt-get update && apt-get install -y --no-install-recommends curl gcc g++ && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace
# vLLM 0.15.1：首个提供运行期 LoRA 热加载 API 的版本线（POST /v1/load_lora_adapter、
# POST /v1/unload_lora_adapter，vllm/entrypoints/serve/lora/）。
# 实测 0.8.5/0.9.2/0.10.2/0.11.2/0.12.0/0.13.0 均无该 API（PUT/POST /v1/loras 全 404）。
# 依赖：torch==2.9.1（硬 pin）、transformers>=4.56,<5（4.57.6 可用，无需降级；
# 注意 0.9.2 反而要求 4.53.2——4.57 的 aimv2 注册与其 Ovis 配置冲突）。
RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir "vllm==0.15.1"
# transformers 单独一层：版本调整时无需重装 vllm/torch（构建缓存复用）
RUN pip install --no-cache-dir "huggingface_hub" "transformers>=4.56,<5"

CMD ["sleep", "infinity"]

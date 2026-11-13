FROM python:3.10-slim

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/data/hf-cache \
    TOKENIZERS_PARALLELISM=false \
    WANDB_MODE=disabled

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# torch 默认 PyPI 轮子自带 CUDA 12.x，适配 3080(Ampere)/4090
RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir "torch==2.5.1" \
    && pip install --no-cache-dir "llamafactory>=0.9,<0.10" \
    && pip install --no-cache-dir "tensorboard"

# DeepSpeed ZeRO 配置模板（训练任务会按需复制到作业目录）
COPY ds_z2.json ds_z3.json /workspace/

CMD ["sleep", "infinity"]

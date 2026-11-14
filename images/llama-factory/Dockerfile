# 兜底镜像：默认训练镜像是 LLaMA-Factory 官方预构建 hiyouga/llamafactory:0.9.5
# （compose IMAGES_TRAIN），仅在离线/气隔环境无法拉取官方镜像时，用本 Dockerfile
# 自构 fintune/llama-factory:1.0 并把 IMAGES_TRAIN 指回它。
FROM python:3.12-slim

ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    DEBIAN_FRONTEND=noninteractive \
    HF_HOME=/data/hf-cache \
    TOKENIZERS_PARALLELISM=false \
    WANDB_MODE=disabled

RUN apt-get update && apt-get install -y --no-install-recommends git build-essential && rm -rf /var/lib/apt/lists/*

WORKDIR /workspace

# LLaMA-Factory 0.9.5（2026 最新版，要求 Python>=3.11）。
# torch/transformers/datasets/torchvision 交由 pip 自动解析到最新兼容版本
# （torch 2.15.x 默认 wheel 为 CUDA 13.0 构建，驱动需 ≥595/推荐 620；
# transformers 上限 5.6.0 由 llamafactory 自身约束保证）。
RUN pip install --no-cache-dir -U pip setuptools wheel \
    && pip install --no-cache-dir "llamafactory==0.9.5" \
    && pip install --no-cache-dir "tensorboard"

# DeepSpeed ZeRO 配置模板（训练任务会按需复制到作业目录）
COPY ds_z2.json ds_z3.json /workspace/

CMD ["sleep", "infinity"]

# 离线/气隔环境自构兜底镜像（fintune/evalscope:1.0）。
# 默认不再使用：评测引擎已切官方开源 ModelScope 镜像（内置 evalscope/openai/
# jieba/rouge-chinese，config.images_eval / compose IMAGES_EVAL 指向，首次评测
# 任务时懒加载拉取；平台 runner.py 由 backend 运行时投递，无需本镜像）。
# 离线环境启用方式：deploy/build-images.sh 恢复被注释的 build 行 + compose
# 将 IMAGES_EVAL 覆盖为 fintune/evalscope:1.0（ensure_image 自动走 /repo 构建）。
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

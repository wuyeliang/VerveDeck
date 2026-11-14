#!/usr/bin/env bash
# VerveDeck 平台镜像构建脚本（幂等，可重复执行）
#
# 部署阶段只构建平台自身依赖的镜像（compose 服务 + 评测/标注/解析/监控）。
# 任务引擎镜像（vllm/sglang/llamafactory）不在部署时下载——
# 页面上创建训练/推理任务时由 backend 检测本地缺失再自动拉取/构建（懒加载，
# 见 backend app/services/image_ops.py）。
set -euo pipefail
cd "$(dirname "$0")/.."   # 项目根目录

build() {
  local ctx="$1" img="$2"
  if docker image inspect "$img" >/dev/null 2>&1 && [ "${REBUILD:-0}" != "1" ]; then
    echo "==> 跳过 $img（已存在；REBUILD=1 可强制重建）"
    return 0
  fi
  echo "==> 构建 $img (context: $ctx)"
  docker build -t "$img" "$ctx"
}

# 评测引擎（evalscope + 平台 runner）
build images/evalscope    fintune/evalscope:1.0
# 数据标注（compose 服务，同样在此统一构建便于一键）
build images/argilla      fintune/argilla:1.0
# PDF 解析（compose 服务，占 1 张 GPU）
build images/mineru       fintune/mineru:1.0
# GPU 监控 Agent（DCGM 容器内采集，compose 服务）
build images/gpu-agent    fintune/gpu-agent:1.0

# 任务引擎镜像（按需，部署时不下载）：
#   vllm/vllm-openai:v0.33.0      官方开源（CUDA 13.5 内置）
#   lmsysorg/sglang:v0.5.23       官方开源（CUDA 13.0 内置）
#   fintune/llama-factory:1.0     自构（images/llama-factory，首次训练任务时构建）

echo ""
echo "平台镜像就绪："
docker images | grep -E "REPOSITORY|fintune" || true

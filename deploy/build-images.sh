#!/usr/bin/env bash
# VerveDeck 任务镜像构建脚本（幂等，可重复执行）
# 训练/推理/评测等任务镜像不是 compose 服务，由 backend 通过 docker API 按需创建容器，
# 因此必须先在本机构建并打标签，供 taskcontainer.create_container 引用。
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

# LLaMA-Factory 训练引擎（含 DeepSpeed ZeRO 配置模板）
build images/llama-factory fintune/llama-factory:1.0
# 推理引擎
build images/vllm         fintune/vllm:1.0
build images/sglang       fintune/sglang:1.0
# 评测引擎（evalscope + 平台 runner）
build images/evalscope    fintune/evalscope:1.0
# 数据标注（compose 服务，同样在此统一构建便于一键）
build images/argilla      fintune/argilla:1.0
# PDF 解析（compose 服务，占 1 张 GPU）
build images/mineru       fintune/mineru:1.0

echo ""
echo "全部任务镜像就绪："
docker images | grep -E "REPOSITORY|fintune" || true

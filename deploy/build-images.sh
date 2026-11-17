#!/usr/bin/env bash
# VerveDeck 平台镜像构建脚本（幂等，可重复执行）
#
# 部署阶段只构建平台自身依赖的镜像（compose 服务 + 评测/标注/解析/监控）。
# 任务引擎镜像（vllm/sglang/llamafactory）不在部署时下载——
# 页面上创建训练/推理任务时由 backend 检测本地缺失再自动拉取/构建（懒加载，
# 见 backend app/services/image_ops.py）。
set -euo pipefail
cd "$(dirname "$0")/.."   # 项目根目录
# 读取 deploy/.env（若存在）：尊重用户覆盖（如 IMAGES_EVAL）；不存在的变量不报错
if [ -f deploy/.env ]; then
  set -a
  # shellcheck disable=SC1091
  . ./deploy/.env
  set +a
fi

build() {
  local ctx="$1" img="$2"
  if docker image inspect "$img" >/dev/null 2>&1 && [ "${REBUILD:-0}" != "1" ]; then
    echo "==> 跳过 $img（已存在；REBUILD=1 可强制重建）"
    return 0
  fi
  echo "==> 构建 $img (context: $ctx)"
  docker build -t "$img" "$ctx"
}

# 评测引擎：已切官方开源 ModelScope 镜像（内置 evalscope/openai/jieba/rouge-chinese，
# 免本地编译；平台 runner.py 运行时由 backend 投递进容器；tag 与 compose IMAGES_EVAL
# 缺省值一致）。部署时预拉取——镜像含 CUDA+torch 约 15GB+，懒加载首评场景下载时长
# 远超任务等待窗口（全新机器首评必然卡 pulling 失败）；backend 懒加载仅作兜底。
# images/evalscope 保留为离线/气隔环境自构兜底——需要时恢复下面一行的 build 并把
# compose 的 IMAGES_EVAL 覆盖为 fintune/evalscope:1.0
EVAL_IMG="${IMAGES_EVAL:-modelscope-registry.cn-hangzhou.cr.aliyuncs.com/modelscope-repo/modelscope:ubuntu22.04-cuda12.4.0-py311-torch2.6.0-1.26.0-LLM}"
if ! docker image inspect "$EVAL_IMG" >/dev/null 2>&1; then
  echo "==> 拉取评测引擎镜像（较大，约 15GB+）"
  docker pull "$EVAL_IMG"
fi
# build images/evalscope    fintune/evalscope:1.0
# 数据标注：已切官方镜像 argilla/argilla-server:v2.8.0（部署时确保已拉取）；
# images/argilla 保留为离线/气隔环境自构兜底——需要时恢复下面两行并去掉 compose 里的官方 image
# build images/argilla      fintune/argilla:1.0
if ! docker image inspect argilla/argilla-server:v2.8.0 >/dev/null 2>&1; then
  echo "==> 拉取 argilla/argilla-server:v2.8.0"
  docker pull argilla/argilla-server:v2.8.0
fi
# PDF 解析（compose 服务，占 1 张 GPU）
build images/mineru       fintune/mineru:1.0
# GPU 监控 Agent：直接用 NVIDIA 开源 nvidia/dcgm 镜像（本地零构建），部署时确保已拉取
if ! docker image inspect nvidia/dcgm:4.7.1-ubuntu24.04 >/dev/null 2>&1; then
  echo "==> 拉取 nvidia/dcgm:4.7.1-ubuntu24.04"
  docker pull nvidia/dcgm:4.7.1-ubuntu24.04
fi

# 任务引擎镜像（按需，部署时不下载）：
#   vllm/vllm-openai:v0.33.0        官方开源（CUDA 13.5 内置）
#   lmsysorg/sglang:v0.5.23         官方开源（CUDA 13.0 内置）
#   hiyouga/llamafactory:0.9.5      LLaMA-Factory 官方预构建（首次训练任务时拉取；
#                                   离线/气隔环境可用 images/llama-factory 自构兜底）

echo ""
echo "平台镜像就绪："
docker images | grep -E "REPOSITORY|fintune" || true

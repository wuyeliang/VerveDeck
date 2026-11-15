#!/bin/bash
# VerveDeck 节点心跳 Agent（Prometheus 重构后）
# GPU/系统指标采集已整体移交 Prometheus 链路（dcgm-exporter + node-exporter），
# 本容器只保留两件事：
#   /data/gpu/agent.ts     节点心跳时间戳（backend 据此判定节点在线/离线并发事件）
#   /data/gpu/agent.names  GPU 型号登记（backend 节点表展示，index,name）
#   /data/gpu/agent.meta   节点环境：驱动 / CUDA 版本（backend 节点卡展示）
set -u
OUT=/data/gpu
mkdir -p "$OUT"

# GPU 型号（nvidia-smi 查询一次即可；失败不致命，backend 按空名兜底）
nvidia-smi --query-gpu=index,name --format=csv,noheader > "$OUT/agent.names" 2>/dev/null || true

# 驱动 / CUDA 版本（nvidia-smi 采集一次即可；失败不致命，backend 按空值兜底展示 -）
{
  drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
  cuda=$(nvidia-smi 2>/dev/null | awk -F': ' '/CUDA Version/ {print $2; exit}')
  echo "driver=${drv:-}"
  echo "cuda=${cuda:-}"
} > "$OUT/agent.meta" 2>/dev/null || true

# 心跳循环（5s）
while true; do
  date +%s > "$OUT/agent.ts"
  sleep 5
done

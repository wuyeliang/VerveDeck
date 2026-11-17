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

# 驱动 / CUDA 版本 / IB 设备（采集一次即可；失败不致命，backend 按空值兜底展示 -）
# ib_devices：宿主 IB/RDMA 设备名（uverbsN/rdma_cm 逗号分隔），无 RDMA 网卡时为空。
# 依赖 compose 把宿主 /dev/infiniband 只读挂入本容器（无 IB 的宿主挂出空目录）。
{
  drv=$(nvidia-smi --query-gpu=driver_version --format=csv,noheader 2>/dev/null | head -1 | tr -d ' ')
  cuda=$(nvidia-smi 2>/dev/null | awk -F': ' '/CUDA Version/ {print $2; exit}')
  ib=$(ls /dev/infiniband 2>/dev/null | grep -E '^(uverbs[0-9]+|rdma_cm)$' | paste -sd, -)
  echo "driver=${drv:-}"
  echo "cuda=${cuda:-}"
  echo "ib_devices=${ib:-}"
} > "$OUT/agent.meta" 2>/dev/null || true

# 心跳循环（5s）
while true; do
  date +%s > "$OUT/agent.ts"
  sleep 5
done

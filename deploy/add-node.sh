#!/usr/bin/env bash
# ============================================================================
# VerveDeck 添加计算节点（在【新机器】上运行，把本机注册进集群管理面）
#
# 功能：
#   1. 校验本机 GPU（nvidia-smi）
#   2. 登录平台获取 token
#   3. POST /api/cluster/nodes 注册节点（名称/卡数/卡名/驱动/CUDA 写入 meta）
#   4. 回查确认节点在册
#
# 用法（在新机器上，可先 scp 本脚本过去）：
#   VD_PLATFORM=https://100.100.33.86 \
#   VD_USER=admin VD_PASS=admin123 \
#   bash add-node.sh [节点名]          # 节点名缺省 = 本机 hostname
#
# 说明：
#   - 幂等：节点名已存在时报错退出（先删旧节点或用 update 改卡数）
#   - 若还要在新机器上跑训练/推理任务：需在该机再部署一套平台栈
#     （sudo bash install-from-iso.sh），其节点由 gpu-agent 心跳自动纳管，
#     跨机训练由平台调度器按「节点×卡数」分配（torchrun 容器网络）
#   - 自签证书自动 -k 跳过校验
# ============================================================================
set -uo pipefail

PLATFORM="${VD_PLATFORM:?用法: VD_PLATFORM=https://<ip> [VD_USER=.. VD_PASS=.. | VD_TOKEN=..] bash add-node.sh [节点名]}"
NODE_NAME="${1:-$(hostname)}"
USER_="${VD_USER:-admin}"
PASS="${VD_PASS:-}"
TOKEN="${VD_TOKEN:-}"

[ "$PLATFORM" = "${PLATFORM%/}" ] && PLATFORM="$PLATFORM"

die() { echo "[error] $*" >&2; exit 1; }

# 1) 本机 GPU 校验
command -v nvidia-smi >/dev/null 2>&1 || die "本机无 nvidia-smi（未装 NVIDIA 驱动？）"
GPU_COUNT=$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ')
[ "$GPU_COUNT" -gt 0 ] || die "nvidia-smi 未发现 GPU"
GPU_NAMES=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | paste -sd',' -)
DRIVER=$(nvidia-smi --query-gpu-driver-version --format=csv,noheader 2>/dev/null | head -1)
CUDA=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9.]+' | awk '{print $3}')
echo "本机: $NODE_NAME  GPU×$GPU_COUNT ($GPU_NAMES)  驱动=$DRIVER  CUDA=$CUDA"

# 2) token
if [ -z "$TOKEN" ]; then
  [ -n "$PASS" ] || die "需提供 VD_PASS（或 VD_TOKEN）"
  TOKEN=$(curl -sk "$PLATFORM/api/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$USER_\",\"password\":\"$PASS\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))') \
    || die "登录请求失败"
  [ -n "$TOKEN" ] || die "登录失败（账号/密码错误？）"
fi
AUTH="Authorization: Bearer $TOKEN"

# 3) 注册节点（含 IB/RDMA HCA 信息采集）
IB_HCAS=$(ls /sys/class/infiniband/ 2>/dev/null | paste -sd',' -)
RDMA_NETS=""
if [ -n "$IB_HCAS" ]; then
  # 每个 HCA 关联的网口（/sys/class/infiniband/<hca>/ports/1/gids 存在即有口）
  RDMA_NETS=$(for h in $IB_HCAS; do
    ip -br link show 2>/dev/null | awk -v h="$h" '{print $1}' | head -5 | paste -sd' ' -
  done | head -1)
fi
META=$(python3 - "$GPU_NAMES" "$DRIVER" "$CUDA" "$IB_HCAS" <<'PY'
import json, sys
names = [x.strip() for x in sys.argv[1].split(',') if x.strip()]
meta = {"names": names, "driver": sys.argv[2] or "", "cuda": sys.argv[3] or ""}
hcas = [x.strip() for x in sys.argv[4].split(',') if x.strip()]
if hcas:
    meta["ib_hca"] = hcas
print(json.dumps(meta, ensure_ascii=False))
PY
)
[ -n "$IB_HCAS" ] && echo "IB HCA: $IB_HCAS（将写入节点 meta，供跨机 RDMA 训练调度参考）"
echo "POST $PLATFORM/api/cluster/nodes  name=$NODE_NAME gpu_count=$GPU_COUNT"
RESP=$(curl -sk -X POST "$PLATFORM/api/cluster/nodes" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{\"name\":\"$NODE_NAME\",\"gpu_count\":$GPU_COUNT,\"status\":\"online\",\"meta\":$META}")
echo "$RESP" | grep -q '"id"' || die "注册失败: $RESP"
NODE_ID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')

# 4) 回查确认
echo "回查节点 #$NODE_ID："
curl -sk "$PLATFORM/api/cluster/nodes" -H "$AUTH" | python3 -c "
import sys, json
for n in json.load(sys.stdin):
    if n['id'] == $NODE_ID:
        print(f\"  {n['name']}  gpu={n['gpu_count']}  status={n['status']}  heartbeat={n.get('last_heartbeat') or '-'}\")
"
echo
echo "完成：节点 $NODE_NAME 已注册进集群。"
echo "页面「集群运维 → 计算节点」可见；任务调度器按卡数分配跨机训练。"

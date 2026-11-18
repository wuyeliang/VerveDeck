#!/usr/bin/env bash
# ============================================================================
# VerveDeck 添加计算节点（在【新机器】上运行，把本机注册进集群管理面）
#
# 参照 GPUStack 节点纳管模型：
#   1. 环境检查：nvidia-smi / GPU 卡 / 到平台的连通性
#   2. 凭服务端签发的注册令牌 VD_TOKEN 认证（页面「添加节点」向导生成接入命令）；
#      兼容旧方式 VD_USER/VD_PASS 登录取用户 token
#   3. POST /api/cluster/nodes 注册节点（自动采集卡数/卡名/驱动/CUDA/IB 写入 meta，
#      节点名以令牌绑定为准；幂等——重启后重跑本脚本即恢复）
#   4. 启动后台心跳守护（nohup）：每 10s 上报心跳 + 实时 GPU 快照，
#      平台据此判定在线/离线（宽限 90s）；PID 文件防重复启动
#
# 用法（在新机器上，可先 scp 本脚本过去）：
#   # 推荐：页面生成的接入命令（内嵌令牌，节点名已绑定）
#   VD_PLATFORM=https://<平台IP>:8443 VD_TOKEN=vdk-xxxx bash add-node.sh
#   # 兼容：账号密码（旧流程）
#   VD_PLATFORM=https://<平台IP>:8443 VD_USER=admin VD_PASS=<密码> bash add-node.sh [节点名]
#
# 选项：
#   VD_DAEMON=0         不启动后台心跳（仅注册；节点状态将不再实时更新）
#   VD_HEARTBEAT_SECS=N 心跳间隔秒数（默认 10；平台离线宽限 90s）
#
# 心跳守护进程：
#   - 日志：/var/log/vervedeck-node-agent.log（仅记录启停与失败，正常心跳不落日志）
#   - 停止：kill $(cat /var/run/vervedeck-node-agent.pid) 或
#           pkill -f vervedeck-node-agent
#
# 说明：
#   - 自签证书自动 -k 跳过校验
#   - 若还要在新机器上跑训练/推理任务：需在该机再部署一套平台栈
#     （sudo bash install-from-iso.sh），其节点由 gpu-agent 心跳自动纳管，
#     跨机训练由平台调度器按「节点×卡数」分配（torchrun 容器网络）
# ============================================================================
set -uo pipefail

PLATFORM="${VD_PLATFORM:?用法: VD_PLATFORM=https://<ip>:8443 [VD_TOKEN=.. | VD_USER=.. VD_PASS=..] bash add-node.sh [节点名]}"
PLATFORM="${PLATFORM%/}"
NODE_NAME="${1:-$(hostname)}"
USER_="${VD_USER:-}"
PASS="${VD_PASS:-}"
TOKEN="${VD_TOKEN:-}"
DAEMON="${VD_DAEMON:-1}"
HB_SECS="${VD_HEARTBEAT_SECS:-10}"

die() { echo "[error] $*" >&2; exit 1; }

# 1) 环境检查（参照 GPUStack "Check Environment" 步骤）
command -v nvidia-smi >/dev/null 2>&1 || die "本机无 nvidia-smi（未装 NVIDIA 驱动？）"
GPU_COUNT=$(nvidia-smi -L 2>/dev/null | wc -l | tr -d ' ')
[ "$GPU_COUNT" -gt 0 ] || die "nvidia-smi 未发现 GPU"
GPU_NAMES=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | paste -sd',' -)
DRIVER=$(nvidia-smi --query-gpu-driver-version --format=csv,noheader 2>/dev/null | head -1)
CUDA=$(nvidia-smi 2>/dev/null | grep -oE 'CUDA Version: [0-9.]+' | awk '{print $3}')
echo "环境检查 OK: $NODE_NAME  GPU×$GPU_COUNT ($GPU_NAMES)  驱动=$DRIVER  CUDA=$CUDA"
curl -sk -m 5 -o /dev/null "$PLATFORM/api/auth/login" || die "无法连通平台 $PLATFORM（端口/防火墙？）"

# 2) 认证：优先注册令牌（页面签发，24h 有效）；否则账号登录（兼容旧流程）
if [ -z "$TOKEN" ]; then
  [ -n "$USER_" ] && [ -n "$PASS" ] || die "需提供 VD_TOKEN（推荐，页面「添加节点」生成接入命令）或 VD_USER+VD_PASS"
  TOKEN=$(curl -sk "$PLATFORM/api/auth/login" -H 'Content-Type: application/json' \
    -d "{\"username\":\"$USER_\",\"password\":\"$PASS\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("token",""))') \
    || die "登录请求失败"
  [ -n "$TOKEN" ] || die "登录失败（账号/密码错误？）"
  echo "已用账号 $USER_ 登录（提示：令牌方式更推荐——页面「添加节点」生成接入命令）"
fi
AUTH="Authorization: Bearer $TOKEN"

# 3) 采集 IB/RDMA 并注册节点（节点名以令牌绑定为准；meta 携带环境快照）
IB_HCAS=$(ls /sys/class/infiniband/ 2>/dev/null | paste -sd',' -)
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
# 携带 token 时不传 name（服务端以令牌绑定的节点名为准）
NAME_ARG=""
case "$TOKEN" in vdk-*) ;; *) NAME_ARG="\"name\":\"$NODE_NAME\"," ;; esac
echo "POST $PLATFORM/api/cluster/nodes  (节点: ${TOKEN:vdk-*:$(printf '%.4s' "$TOKEN")}***) gpu_count=$GPU_COUNT"
RESP=$(curl -sk -X POST "$PLATFORM/api/cluster/nodes" -H "$AUTH" -H 'Content-Type: application/json' \
  -d "{$NAME_ARG\"gpu_count\":$GPU_COUNT,\"status\":\"online\",\"meta\":$META,\"token\":\"$TOKEN\"}")
echo "$RESP" | grep -q '"id"' || die "注册失败: $RESP"
NODE_ID=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
REG_NAME=$(echo "$RESP" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("name",""))')
[ -n "$REG_NAME" ] && NODE_NAME="$REG_NAME"

# 4) 回查确认
echo "回查节点 #$NODE_ID："
curl -sk "$PLATFORM/api/cluster/nodes" -H "$AUTH" | python3 -c "
import sys, json
for n in json.load(sys.stdin):
    if n['id'] == $NODE_ID:
        print(f\"  {n['name']}  gpu={n['gpu_count']}  status={n['status']}  heartbeat={n.get('last_heartbeat') or '-'}\")
"

# 5) 启动后台心跳守护（幂等：已在运行则跳过）
if [ "$DAEMON" = "1" ]; then
  RUN_DIR=/var/run; [ -w "$RUN_DIR" ] || RUN_DIR=/tmp
  LOG_FILE=/var/log/vervedeck-node-agent.log; [ -w /var/log ] || LOG_FILE="$HOME/.vervedeck-node-agent.log"
  PID_FILE="$RUN_DIR/vervedeck-node-agent.pid"
  if [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null; then
    echo "心跳守护已在运行（pid $(cat "$PID_FILE")），跳过启动"
  else
    nohup bash -c '
TOKEN="'"$TOKEN"'"
PLATFORM="'"$PLATFORM"'"
NODE_ID="'"$NODE_ID"'"
SECS="'"$HB_SECS"'"
LOG_FILE="'"$LOG_FILE"'"
PID_FILE="'"$PID_FILE"'"
echo $$ > "$PID_FILE"
echo "$(date "+%F %T") vervedeck-node-agent 启动 node_id=$NODE_ID 间隔=${SECS}s" >> "$LOG_FILE"
trap "echo \"\$(date \"+%F %T\") 心跳守护退出\" >> \"$LOG_FILE\"" EXIT
while :; do
  GPU_COUNT=$(nvidia-smi -L 2>/dev/null | wc -l | tr -d " ")
  GPU_NAMES=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | paste -sd"," -)
  DRIVER=$(nvidia-smi --query-gpu-driver-version --format=csv,noheader 2>/dev/null | head -1)
  CUDA=$(nvidia-smi 2>/dev/null | grep -oE "CUDA Version: [0-9.]+" | awk "{print \$3}")
  IB=$(ls /sys/class/infiniband/ 2>/dev/null | paste -sd"," -)
  PAYLOAD=$(python3 -c "
import json, sys
d = {\"token\": sys.argv[1]}
try: d[\"gpu_count\"] = int(sys.argv[2])
except Exception: pass
if sys.argv[3]: d[\"names\"] = [x for x in sys.argv[3].split(\",\") if x]
if sys.argv[4]: d[\"driver\"] = sys.argv[4]
if sys.argv[5]: d[\"cuda\"] = sys.argv[5]
if sys.argv[6]: d[\"ib_devices\"] = sys.argv[6]
print(json.dumps(d, ensure_ascii=False))
" "$TOKEN" "${GPU_COUNT:-0}" "$GPU_NAMES" "$DRIVER" "$CUDA" "$IB")
  curl -sk -m 5 -X POST "$PLATFORM/api/cluster/nodes/$NODE_ID/heartbeat" \
    -H 'Content-Type: application/json' -d "$PAYLOAD" >/dev/null 2>&1 \
    || echo "$(date "+%F %T") 心跳上报失败（平台不可达？）$PLATFORM" >> "$LOG_FILE"
  sleep "$SECS"
done' >> "$LOG_FILE" 2>&1 &
    sleep 1
    [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE" 2>/dev/null)" 2>/dev/null \
      && echo "心跳守护已启动（pid $(cat "$PID_FILE")，间隔 ${HB_SECS}s，日志 $LOG_FILE）" \
      || echo "[warn] 心跳守护启动失败（查 $LOG_FILE）"
  fi
fi

echo
echo "完成：节点 $NODE_NAME 已注册进集群。"
echo "页面「集群运维 → 计算节点」可见实时状态；任务调度器按卡数分配跨机训练。"
echo "停止心跳：kill \$(cat ${RUN_DIR:-/var/run}/vervedeck-node-agent.pid)；删除节点后本进程会自动失去注册资格。"

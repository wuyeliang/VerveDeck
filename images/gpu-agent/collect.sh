#!/bin/bash
# VerveDeck GPU + Linux 系统监控采集（5s 周期）
# 输出（backend 读取）：
#   /data/gpu/agent.csv   列序固定：index + 下列字段（列名与 backend EXTRA_KEYS 对齐，缺列按 0 兜底）
#   /data/gpu/agent.ts    unix 时间戳（节点心跳，仅在采到有效数据时刷新，避免误判离线）
#   /data/gpu/agent.names GPU 型号列表（nvidia-smi 注入，index,name）
#   /data/gpu/host.csv    宿主 Linux 系统指标（CPU/内存/磁盘/负载/网络；容器 /proc 无 lxcfs
#                         时为宿主级数据；网络需 compose 配 network_mode: host 才是宿主网卡）
#
# 注意：DCGM 字段 ID 随版本变化（本镜像官方 nvidia/dcgm 4.7.1）。下列 ID 已在
# 4.7.1（及此前 3.3.9）逐一经 `dcgmi dmon -c 1 -i 0 -e <id>` 验证可用；
# `dcgm_fields.h` 为准。旧版脚本硬编码的 239/265/282/204 等 ID 在 3.3.9+ 已不
# 存在，会导致 "invalid field ID" 而整表采不到。
set -u
OUT=/data/gpu
mkdir -p "$OUT"

# DCGM 4.7.1 有效字段（顺序即 CSV 数据列序，列名与 backend EXTRA_KEYS 对齐）：
#  温度/功耗   GPU_TEMP=150  POWER_USAGE=155  POWER_MGMT_LIMIT=160
#  利用率      GPU_UTIL=203
#  显存温度    MEMORY_TEMP=140
#  累计能耗    TOTAL_ENERGY_CONSUMPTION=156
#  ECC(总计)   DBE_AGG_TOTAL=313  SBE_AGG_TOTAL=312  DBE_VOL_TOTAL=311  SBE_VOL_TOTAL=310
#  显存(MiB)   FB_FREE=251  FB_USED=252  FB_TOTAL=250
#  时钟(MHz)   SM_CLOCK=100  MEM_CLOCK=101  VIDEO_CLOCK=102
#  健康/错误   ROW_REMAP_PENDING=396  XID_ERRORS=230
#  （本 GPU/版本不支持 enc/dec/mem_copy 利用率、细粒度降频原因、health 位，backend 按 0 兜底）
FIELDS="150,155,160,203,140,156,313,312,311,310,251,252,250,100,101,102,396,230"
NFIELDS=18
HEADER="index,temp,power,power_limit,gpu_util,mem_temp,total_energy,ecc_dbe_agg,ecc_sbe_agg,ecc_dbe_vol,ecc_sbe_vol,fb_free,fb_used,fb_total,sm_clock,mem_clock,video_clock,row_remap_pending,xid_error_count"

# 等待 hostengine 就绪（入口已拉起；最多等 2 分钟）
for _ in $(seq 1 120); do
  dcgmi discovery -l >/dev/null 2>&1 && break
  sleep 1
done

# GPU 索引列表（dcgmi dmon -i 需数值 entityId，不接受 ALL；单卡=0，多卡=0,1,...）
GPU_IDS=$(nvidia-smi --query-gpu=index --format=csv,noheader 2>/dev/null | paste -sd, -)
[ -z "$GPU_IDS" ] && GPU_IDS="0"

# GPU 型号（容器运行时注入的 nvidia-smi；失败不致命，backend 按空名兜底）
nvidia-smi --query-gpu=index,name --format=csv,noheader > "$OUT/agent.names" 2>/dev/null || true

# ---------- 宿主 Linux 系统指标（/proc 无 lxcfs 时为宿主级；网络需 host 网络） ----------
HOST_HEADER="ts,cpu_pct,cpu_count,load1,load5,load15,mem_total_mb,mem_used_mb,mem_pct,disk_total_mb,disk_used_mb,disk_pct,net_rx_mbps,net_tx_mbps"
CPU_PREV=/tmp/.host_cpu_prev
NET_PREV=/tmp/.host_net_prev
HOST_TMP="$OUT/.host.csv.tmp"

collect_host() {
  local ts cpu_count load1 load5 load15 mem_total mem_avail mem_used mem_pct
  local disk_total disk_used disk_pct rx_bytes tx_bytes rx_rate tx_rate
  ts=$(date +%s)
  cpu_count=$(nproc 2>/dev/null || echo 0)

  # CPU：/proc/stat 两次采样差值（user nice system idle iowait irq softirq steal）
  local cpu_total cpu_idle cpu_pct=0
  local u n s i io irq sirq st
  read -r u n s i io irq sirq st < <(awk '/^cpu /{print $2,$3,$4,$5,$6,$7,$8,$9}' /proc/stat)
  cpu_total=$((u+n+s+i+io+irq+sirq+st))
  cpu_idle=$((i+io))
  if [ -f "$CPU_PREV" ]; then
    local p_ts p_total p_idle d_total d_idle
    read -r p_ts p_total p_idle < "$CPU_PREV"
    d_total=$((cpu_total - p_total))
    d_idle=$((cpu_idle - p_idle))
    if [ "$d_total" -gt 0 ]; then
      cpu_pct=$(( (d_total - d_idle) * 100 / d_total ))
      [ "$cpu_pct" -lt 0 ] && cpu_pct=0
      [ "$cpu_pct" -gt 100 ] && cpu_pct=100
    fi
  fi
  echo "$ts $cpu_total $cpu_idle" > "$CPU_PREV"

  # 负载
  read -r load1 load5 load15 _ < /proc/loadavg

  # 内存（MB）：used = total - available
  mem_total=$(awk '/^MemTotal:/{print int($2/1024)}' /proc/meminfo)
  mem_avail=$(awk '/^MemAvailable:/{print int($2/1024)}' /proc/meminfo)
  mem_used=$((mem_total - mem_avail))
  if [ "$mem_total" -gt 0 ]; then mem_pct=$(( mem_used * 100 / mem_total )); else mem_pct=0; fi

  # 磁盘（MB）：根分区（host 网络 + 挂载卷反映宿主）
  local df_total df_used
  df_total=$(df -B1 / 2>/dev/null | awk 'NR==2{print int($2/1024/1024)}')
  df_used=$(df -B1 / 2>/dev/null | awk 'NR==2{print int($3/1024/1024)}')
  disk_total=${df_total:-0}; disk_used=${df_used:-0}
  if [ "$disk_total" -gt 0 ]; then disk_pct=$(( disk_used * 100 / disk_total )); else disk_pct=0; fi

  # 网络（Mbps）：全部非 lo 网卡 rx/tx 字节差值
  read -r rx_bytes tx_bytes < <(awk -F'[: ]+' 'NR>2 { if ($1!="lo" && $1!="*") { rx+=$3; tx+=$10 } } END { printf "%d %d", rx+0, tx+0 }' /proc/net/dev)
  rx_rate=0; tx_rate=0
  if [ -f "$NET_PREV" ]; then
    local n_ts n_rx n_tx ndt
    read -r n_ts n_rx n_tx < "$NET_PREV"
    ndt=$((ts - n_ts))
    if [ "$ndt" -gt 0 ]; then
      rx_rate=$(( (rx_bytes - n_rx) * 8 / ndt / 1000000 ))
      tx_rate=$(( (tx_bytes - n_tx) * 8 / ndt / 1000000 ))
      [ "$rx_rate" -lt 0 ] && rx_rate=0
      [ "$tx_rate" -lt 0 ] && tx_rate=0
    fi
  fi
  echo "$ts $rx_bytes $tx_bytes" > "$NET_PREV"

  { echo "$HOST_HEADER"; printf '%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s\n' \
      "$ts" "$cpu_pct" "$cpu_count" "$load1" "$load5" "$load15" \
      "$mem_total" "$mem_used" "$mem_pct" \
      "$disk_total" "$disk_used" "$disk_pct" \
      "$rx_rate" "$tx_rate"; } > "$HOST_TMP" && mv "$HOST_TMP" "$OUT/host.csv"
}

TMP="$OUT/.agent.csv.tmp"
while true; do
  # dmon 单快照：数据行以 "GPU <idx>" 开头，字段按 FIELDS 顺序排列（N/A 原样保留，backend 按 0 处理）
  data=$(dcgmi dmon -c 1 -i "$GPU_IDS" -e "$FIELDS" 2>/dev/null \
    | awk -v n="$NFIELDS" '/^GPU [0-9]+/ { out=$2; for (i=0; i<n; i++) out = out "," $(3+i); print out }')
  # 仅在采到有效 GPU 行时落盘并刷新心跳（瞬时故障保留旧值，避免 CSV 写入瞬间误判离线）
  if [ -n "$data" ]; then
    { echo "$HEADER"; printf '%s\n' "$data"; } > "$TMP" && mv "$TMP" "$OUT/agent.csv" && date +%s > "$OUT/agent.ts"
  fi
  # 宿主系统指标独立采集（GPU 缺失时仍上报，系统监控可用）
  collect_host
  sleep 5
done

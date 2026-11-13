#!/bin/bash
# VerveDeck GPU 监控采集（DCGM，5s 周期）
# 输出（backend 读取）：
#   /data/gpu/agent.csv   列序固定：index + 下列字段（列名与 backend EXTRA_KEYS 对齐，缺列按 0 兜底）
#   /data/gpu/agent.ts    unix 时间戳（节点心跳，仅在采到有效数据时刷新，避免误判离线）
#   /data/gpu/agent.names GPU 型号列表（nvidia-smi 注入，index,name）
#
# 注意：DCGM 字段 ID 随版本变化（本镜像 DCGM 3.3.9）。下列 ID 已在 3.3.9 逐一经
# `dcgmi dmon -c 1 -i 0 -e <id>` 验证可用；`dcgm_fields.h` 为准。旧版脚本硬编码的
# 239/265/282/204 等 ID 在 3.3.9 已不存在，会导致 "invalid field ID" 而整表采不到。
set -u
OUT=/data/gpu
mkdir -p "$OUT"

# DCGM 3.3.9 有效字段（顺序即 CSV 数据列序，列名与 backend EXTRA_KEYS 对齐）：
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

TMP="$OUT/.agent.csv.tmp"
while true; do
  # dmon 单快照：数据行以 "GPU <idx>" 开头，字段按 FIELDS 顺序排列（N/A 原样保留，backend 按 0 处理）
  data=$(dcgmi dmon -c 1 -i "$GPU_IDS" -e "$FIELDS" 2>/dev/null \
    | awk -v n="$NFIELDS" '/^GPU [0-9]+/ { out=$2; for (i=0; i<n; i++) out = out "," $(3+i); print out }')
  # 仅在采到有效 GPU 行时落盘并刷新心跳（瞬时故障保留旧值，避免 CSV 写入瞬间误判离线）
  if [ -n "$data" ]; then
    { echo "$HEADER"; printf '%s\n' "$data"; } > "$TMP" && mv "$TMP" "$OUT/agent.csv" && date +%s > "$OUT/agent.ts"
  fi
  sleep 5
done

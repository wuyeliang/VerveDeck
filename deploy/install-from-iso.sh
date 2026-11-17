#!/usr/bin/env bash
# ============================================================================
# VerveDeck 一键部署（从 OS 安装完 ISO 后的裸机状态开始）
#
# 适用：Ubuntu 20.04 / 22.04 / 24.04（x86_64）全新系统
# 流程：
#   Phase 1  系统依赖：Docker CE + compose v2 + NVIDIA 驱动 + nvidia-container-toolkit
#   Phase 2  获取代码：clone 到 /opt/vervedeck（项目一级目录；
#            部署相关代码在项目 deploy/ 子目录内，本脚本最终调用 deploy/deploy.sh）
#   Phase 3  平台部署：.env / /data 目录 / 任务镜像构建 / compose 拉起 / 健康检查
#   Phase 4  输出访问入口
#
# 用法：
#   sudo bash install-from-iso.sh
#   # 可选环境变量：
#   #   VD_REPO_URL      代码仓库（默认 git@github.com:wuyeliang/VerveDeck.git）
#   #   VD_INSTALL_DIR   安装目录（默认 /opt/vervedeck）
#   #   VD_SKIP_DOCKER=1 跳过 Docker 安装（已装时）
#   #   VD_SKIP_DRIVER=1 跳过 NVIDIA 驱动安装（已装且 nvidia-smi 可用时）
#
# 幂等：重复执行安全（已装组件跳过、已 clone 仓库 git pull）。
# ============================================================================
set -uo pipefail

REPO_URL="${VD_REPO_URL:-git@github.com:wuyeliang/VerveDeck.git}"
INSTALL_DIR="${VD_INSTALL_DIR:-/opt/vervedeck}"
SKIP_DOCKER="${VD_SKIP_DOCKER:-0}"
SKIP_DRIVER="${VD_SKIP_DRIVER:-0}"

log()  { echo -e "\n\033[1;32m==== [install-from-iso] $* ====\033[0m"; }
warn() { echo -e "\033[1;33m[warn] $*\033[0m"; }
die()  { echo -e "\033[1;31m[error] $*\033[0m" >&2; exit 1; }

# ---------------------------------------------------------------- Phase 1 ----
log "Phase 1/4 系统依赖"

[ "$(id -u)" = "0" ] || die "请用 root 或 sudo 运行"

# OS 检查
if [ -r /etc/os-release ]; then
  . /etc/os-release
  case "${ID:-}" in
    ubuntu) : ;;
    *) warn "非 Ubuntu 系统（${ID:-未知}），按 Debian 兼容方式继续，如失败请人工处理" ;;
  esac
  warn "OS: ${PRETTY_NAME:-unknown}"
else
  die "无法识别操作系统（缺少 /etc/os-release）"
fi
uname -m | grep -q x86_64 || die "仅支持 x86_64"

export DEBIAN_FRONTEND=noninteractive
apt-get update -y

# 基础工具
if ! command -v git >/dev/null || ! command -v curl >/dev/null; then
  log "安装基础工具（git/curl/ca-certificates）"
  apt-get install -y git curl ca-certificates gnupg build-essential dkms "linux-headers-$(uname -r)" || die "基础工具安装失败"
fi

# Docker CE（阿里云源）+ compose v2
if [ "$SKIP_DOCKER" = "1" ]; then
  warn "VD_SKIP_DOCKER=1，跳过 Docker 安装"
elif ! command -v docker >/dev/null; then
  log "安装 Docker CE（含 compose v2 插件）"
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc \
    || die "Docker GPG 下载失败"
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    || die "Docker 安装失败"
else
  warn "Docker 已安装：$(docker --version)"
fi
systemctl enable --now docker || die "docker 服务启动失败"
docker compose version >/dev/null 2>&1 || die "docker compose v2 插件不可用"

# NVIDIA 驱动（自动选最高可用 5xx）
if [ "$SKIP_DRIVER" = "1" ]; then
  warn "VD_SKIP_DRIVER=1，跳过 NVIDIA 驱动安装"
else
  if nvidia-smi >/dev/null 2>&1; then
    warn "NVIDIA 驱动已可用：$(nvidia-smi --query-gpu-driver-version --format=csv,noheader | head -1)"
  else
    log "安装 NVIDIA 驱动（自动选择 apt 源中最高 5xx 版本）"
    # 锚定正则排除 -open/-server 变体，版本号取 '-' 分隔第 3 段（nvidia|driver|5xx）
    VER=$(apt-cache search -n 'nvidia-driver' | grep -E '^nvidia-driver-5[0-9][0-9]$' | awk -F'-' '{print $3}' | sort -n | tail -1)
    [ -n "$VER" ] || die "apt 源中未找到 nvidia-driver-5xx 包"
    apt-get install -y "nvidia-driver-$VER" || die "NVIDIA 驱动安装失败"
    warn "驱动已安装。若 nvidia-smi 仍不可用，请 reboot 后重新运行本脚本（已装组件会自动跳过）"
  fi
fi

# nvidia-container-toolkit
if ! nvidia-ctk version >/dev/null 2>&1; then
  log "安装 nvidia-container-toolkit"
  curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey \
    | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg || die "toolkit GPG 失败"
  curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
    | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' \
    > /etc/apt/sources.list.d/nvidia-container-toolkit.list || die "toolkit 源写入失败"
  apt-get update -y
  apt-get install -y nvidia-container-toolkit || die "nvidia-container-toolkit 安装失败"
fi
nvidia-ctk runtime configure --runtime=docker
# Docker 29 默认 storage-driver 为 overlayfs：实测 pull 在 unpack 阶段会挂死，固定为 overlay2
python3 -c "
import json
p='/etc/docker/daemon.json'
try: cfg=json.load(open(p))
except Exception: cfg={}
cfg['storage-driver']='overlay2'
json.dump(cfg,open(p,'w'),indent=2)
print('storage-driver pinned to overlay2')
"
systemctl restart docker

# RDMA/IB 用户态（机器带 IB HCA 时装；训练容器会自动注入 /dev/infiniband 设备）
if [ -d /dev/infiniband ] || { command -v lspci >/dev/null && lspci 2>/dev/null | grep -qi infiniband; }; then
  log "检测到 RDMA/IB 网卡，安装 rdma-core / ibverbs-utils / infiniband-diags"
  apt-get install -y rdma-core ibverbs-utils infiniband-diags pciutils \
    || warn "RDMA 用户态安装失败（不影响部署，跨机 RDMA 训练将不可用）"
  warn "RDMA 设备: $(ls /dev/infiniband/ 2>/dev/null | tr '\n' ' ')"
else
  warn "未发现 IB HCA（单机部署无需 RDMA；跨机训练需 IB/RoCE 网卡）"
fi
warn "nvidia-smi 当前状态：$(nvidia-smi >/dev/null 2>&1 && echo OK || echo '不可用（新装驱动需先 reboot）')"
nvidia-smi >/dev/null 2>&1 || die "nvidia-smi 不可用——请先 reboot，再重新运行本脚本（依赖已装好会自动跳过）"

# ---------------------------------------------------------------- Phase 2 ----
log "Phase 2/4 获取代码 → $INSTALL_DIR"

if [ -d "$INSTALL_DIR/.git" ]; then
  warn "仓库已存在，git pull 更新"
  git -C "$INSTALL_DIR" pull --ff-only || warn "git pull 失败（继续使用现有代码）"
else
  mkdir -p "$(dirname "$INSTALL_DIR")"
  git clone "$REPO_URL" "$INSTALL_DIR" || die "git clone 失败（$REPO_URL）"
fi
[ -f "$INSTALL_DIR/deploy/deploy.sh" ] || die "deploy/deploy.sh 不存在（仓库不完整？）"

# ---------------------------------------------------------------- Phase 3 ----
log "Phase 3/4 平台部署（deploy/deploy.sh）"
cd "$INSTALL_DIR/deploy"
bash deploy.sh || die "deploy.sh 执行失败"

# ---------------------------------------------------------------- Phase 4 ----
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
[ -n "$IP" ] || IP="<服务器IP>"
log "Phase 4/4 完成"
cat <<EOF
  ┌────────────────────────────────────────────────────────┐
  │ VerveDeck 部署完成                                      │
  ├────────────────────────────────────────────────────────┤
  │ 平台入口:  https://$IP                                  │
  │ 账号:      admin / admin123（首次登录强制改密）          │
  │ 代码目录:  $INSTALL_DIR（部署代码在 deploy/ 子目录）     │
  │ 数据目录:  /data                                       │
  │ 常用运维:  cd $INSTALL_DIR/deploy && docker compose ps │
  │                                                    │
  │ 文档: $INSTALL_DIR/docs/02-一键部署.md（运维 07）       │
  └────────────────────────────────────────────────────────┘
EOF

#!/bin/bash
# Phase A3: apt --fix-broken 修复依赖后安装 Docker + NVIDIA 驱动 + toolkit
set -x
export DEBIAN_FRONTEND=noninteractive
exec > /root/setup-a3.log 2>&1
echo "=== Phase A3 start $(date)"
dpkg --configure -a || true
apt-get install -f -y || echo "FIX_BROKEN_FAILED"
apt-get update -y

apt-get install -y curl ca-certificates gnupg lsb-release build-essential dkms || echo "BASE_DEPS_FAILED"
apt-get install -y linux-headers-$(uname -r) || echo "WARN headers"

apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || echo "DOCKER_INSTALL_FAILED"

# 版本选择：-n 只输出包名，用锚定正则排除 -open/-server 等变体，版本号取 '-' 分隔第 3 段
VER=$(apt-cache search -n 'nvidia-driver' | grep -E '^nvidia-driver-5[0-9][0-9]$' | awk -F'-' '{print $3}' | sort -n | tail -1)
if [ -z "$VER" ]; then echo "ERROR: no nvidia-driver-5xx package found"; exit 1; fi
echo "selected nvidia driver version: $VER"
apt-get install -y "nvidia-driver-$VER" || { echo "ERROR: driver install failed"; exit 1; }

apt-get install -y nvidia-container-toolkit || echo "TOOLKIT_INSTALL_FAILED"
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
systemctl enable --now docker
sleep 2
docker version --format 'docker {{.Server.Version}}' 2>&1
echo "=== nvidia-smi check ==="
nvidia-smi || echo "NVIDIA_SMI_NEED_REBOOT_OR_VERIFY"
echo "=== Phase A3 done $(date)"

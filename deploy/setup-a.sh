#!/bin/bash
# Phase A: 安装 Docker CE + NVIDIA 驱动 + nvidia-container-toolkit（无需重启部分）
set -x
export DEBIAN_FRONTEND=noninteractive
exec > /root/setup-a.log 2>&1
echo "=== Phase A start $(date)"
if [ -d /sys/firmware/efi ]; then echo "UEFI detected"; fi
mokutil --sb-state 2>/dev/null || echo "mokutil missing (assume legacy/no-secureboot-check)"

apt-get update -y
apt-get install -y curl ca-certificates gnupg lsb-release build-essential dkms
apt-get install -y linux-headers-$(uname -r) || echo "WARN: headers install failed (may still work)"

# Docker CE via aliyun mirror
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://mirrors.aliyun.com/docker-ce/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://mirrors.aliyun.com/docker-ce/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" > /etc/apt/sources.list.d/docker.list
apt-get update -y
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# NVIDIA driver: pick highest available 5xx。
# 注意：apt-cache search -n 只输出包名（如 nvidia-driver-595），版本号是 '-' 分隔的第 3 段
#（nvidia|driver|595）；旧实现取第 2 段会得到 "driver" 导致安装 nvidia-driver-driver 静默失败。
VER=$(apt-cache search -n '^nvidia-driver-5[0-9][0-9]$' | awk -F'-' '{print $3}' | sort -n | tail -1)
if [ -z "$VER" ]; then
  echo "ERROR: apt 源中未找到 nvidia-driver-5xx 包（sources 可能缺 universe）"
  exit 1
fi
echo "selected nvidia driver: $VER"
apt-get install -y "nvidia-driver-$VER" || { echo "ERROR: nvidia-driver-$VER 安装失败"; exit 1; }

# nvidia-container-toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -y
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker
# Docker 29 默认 storage-driver 为 overlayfs：实测其 pull 在 unpack/commit 阶段会挂死
#（层全部 "Pull complete" 后 daemon 无响应，需重启 daemon 才能恢复），固定回经典 overlay2。
python3 - <<'PYEOF'
import json
p = "/etc/docker/daemon.json"
cfg = {}
try:
    with open(p) as f:
        cfg = json.load(f)
except FileNotFoundError:
    pass
cfg["storage-driver"] = "overlay2"
with open(p, "w") as f:
    json.dump(cfg, f, indent=2)
print("storage-driver pinned to overlay2")
PYEOF

systemctl enable --now docker
echo "=== nvidia-smi check (pre-reboot) ==="
nvidia-smi || echo "NVIDIA_SMI_FAILED_NEED_REBOOT"
echo "=== Phase A done $(date)"

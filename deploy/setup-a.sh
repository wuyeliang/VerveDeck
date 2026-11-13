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

# NVIDIA driver: pick highest available 5xx
VER=$(apt-cache search -n '^nvidia-driver-5[6-9][0-9]$' | awk -F'-' '{print $2}' | sort -n | tail -1)
echo "selected nvidia driver: $VER"
apt-get install -y "nvidia-driver-$VER"

# nvidia-container-toolkit
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -fsSL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#' > /etc/apt/sources.list.d/nvidia-container-toolkit.list
apt-get update -y
apt-get install -y nvidia-container-toolkit
nvidia-ctk runtime configure --runtime=docker

systemctl enable --now docker
echo "=== nvidia-smi check (pre-reboot) ==="
nvidia-smi || echo "NVIDIA_SMI_FAILED_NEED_REBOOT"
echo "=== Phase A done $(date)"

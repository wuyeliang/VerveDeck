#!/bin/bash
# Phase A2: 修复 dpkg 中断状态后重装 Docker + NVIDIA 驱动 + toolkit
set -x
export DEBIAN_FRONTEND=noninteractive
exec > /root/setup-a2.log 2>&1
echo "=== Phase A2 start $(date)"
dpkg --configure -a || echo "WARN dpkg configure failed"
apt-get update -y
apt-get install -y curl ca-certificates gnupg lsb-release build-essential dkms
apt-get install -y linux-headers-$(uname -r) || echo "WARN: headers failed"

apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin || echo "DOCKER_INSTALL_FAILED"

VER=$(apt-cache search -n 'nvidia-driver-5[0-9][0-9]' | awk '{print $1}' | sed 's/^nvidia-driver-//' | sort -n | tail -1)
echo "selected nvidia driver version: $VER"
apt-get install -y "nvidia-driver-$VER" || echo "DRIVER_INSTALL_FAILED"

apt-get install -y nvidia-container-toolkit || echo "TOOLKIT_INSTALL_FAILED"
nvidia-ctk runtime configure --runtime=docker
systemctl enable --now docker
sleep 2
docker version --format '{{.Server.Version}}' 2>&1
echo "=== nvidia-smi check ==="
nvidia-smi || echo "NVIDIA_SMI_NEED_REBOOT_OR_VERIFY"
echo "=== Phase A2 done $(date)"

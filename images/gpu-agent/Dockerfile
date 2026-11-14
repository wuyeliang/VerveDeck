# VerveDeck GPU 监控 Agent（DCGM 版）
# 基于 cuda base（与既有 nvidia/cuda 镜像同源，国内拉取稳定）+ CUDA 官方 apt 仓库的
# datacenter-gpu-manager 包（温度/功耗/时钟/ECC/降频/健康度/XID 等扩展指标）。
# 运行要求：nvidia 设备注入（--gpus all）+ ipc: host（DCGM hostengine 共享内存）。
# 2026-11 升级：CUDA 13.5 base（与主机驱动 620 / Ubuntu 24.04 对齐）。
FROM nvidia/cuda:13.5.0-base-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
 && apt-get install -y --no-install-recommends datacenter-gpu-manager \
 && rm -rf /var/lib/apt/lists/*

COPY collect.sh /usr/local/bin/collect.sh
RUN chmod +x /usr/local/bin/collect.sh

# 先后台拉起 DCGM hostengine，再进入 5s 周期采集循环。
# 注意：`nv-hostengine -n` 的 -n 表示"不守护化/前台运行"，会一直阻塞；故必须显式
# 后台化（&），collect.sh 内部再用 `dcgmi discovery -l` 轮询等待 hostengine 就绪。
CMD ["bash", "-lc", "nv-hostengine -n >/tmp/hostengine.log 2>&1 & sleep 2; exec /usr/local/bin/collect.sh"]

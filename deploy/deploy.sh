#!/usr/bin/env bash
# VerveDeck 大模型微调训练平台 - 一键部署
# 用法: sudo bash deploy/deploy.sh
# 前置: 已安装 Docker + docker compose v2 + nvidia-container-toolkit（GPU 驱动由宿主机提供）
set -euo pipefail
cd "$(dirname "$0")"

echo "==> [1/5] 环境预检"
command -v docker >/dev/null || { echo "缺少 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "缺少 docker compose v2"; exit 1; }
if command -v nvidia-smi >/dev/null; then
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader | sed 's/^/    GPU: /'
else
  echo "    警告: 未检测到 nvidia-smi，GPU 训练/推理/MinerU 不可用（CPU 功能正常）"
fi

echo "==> [2/5] 准备配置与环境"
[ -f .env ] || { cp .env.example .env; echo "    已生成 .env（默认值，生产环境请修改密钥）"; }
mkdir -p /data/models/hub /data/datasets /data/jobs /data/evals /data/uploads \
         /data/gpu /data/argilla /data/postgres /data/redis /data/opensearch
# opensearch 容器以 UID 1000 运行，bind 挂载的数据目录必须可写（否则节点启动即 AccessDenied 崩溃）
chown -R 1000:0 /data/opensearch 2>/dev/null || true

# 自签 TLS 证书（Web 强制 HTTPS 443；已有则跳过，生产可替换为正式证书）
mkdir -p tls
if [ ! -f tls/vervedeck.crt ]; then
  openssl req -x509 -nodes -newkey rsa:2048 -days 3650 \
    -keyout tls/vervedeck.key -out tls/vervedeck.crt \
    -subj "/CN=vervedeck.local" \
    -addext "subjectAltName=DNS:vervedeck.local,IP:127.0.0.1" 2>/dev/null
  chmod 600 tls/vervedeck.key
  echo "    已生成自签证书 tls/vervedeck.{crt,key}（浏览器首次访问需信任证书）"
fi

echo "==> [3/5] 构建任务镜像"
bash build-images.sh

echo "==> [4/5] 启动服务栈"
docker compose up -d

echo "==> [5/5] 等待 backend 就绪"
ready=0
for i in $(seq 1 60); do
  if docker compose exec -T backend curl -sf http://127.0.0.1:3001/health >/dev/null 2>&1; then
    ready=1; break
  fi
  sleep 2
done
if [ "$ready" != "1" ]; then
  echo "backend 未在 120s 内就绪，查看日志: docker compose logs backend"
  docker compose ps
  exit 1
fi

# 等待对象存储建桶（rustfs-init 一次性容器）
sleep 3
docker compose exec -T rustfs sh -c "curl -sf http://127.0.0.1:9000/health >/dev/null && echo '    rustfs 健康'" || \
  echo "    警告: rustfs 未就绪（S3 对象存储降级为纯本地）"

echo ""
docker compose ps
echo ""
echo "================ 部署完成 ================"
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "  Web 控制台:     https://${IP:-<服务器IP>}（443 强制 HTTPS，自签证书浏览器需信任）"
echo "  对象存储控制台: http://${IP:-<服务器IP>}:9001"
echo "  S3 API:         http://${IP:-<服务器IP>}:9000"
echo "  初始账号:       admin / ${ADMIN_PASSWORD:-admin123}"
echo "  常用命令:       docker compose -f deploy/docker-compose.yaml ps|logs|down"
echo "=========================================="

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
rand_alnum() { openssl rand -base64 24 | tr -dc 'a-zA-Z0-9' | cut -c1-"${1:-12}"; }
if [ ! -f .env ]; then
  # 首启自动生成强随机密钥（公网部署防默认弱口令被秒破）；.env 权限 600
  umask 077
  {
    echo "# VerveDeck 环境变量 —— deploy.sh 自动生成于 $(date '+%F %T')（请妥善保管本文件）"
    echo "ADMIN_PASSWORD=$(rand_alnum 12)"
    echo "JWT_SECRET=$(openssl rand -hex 32)"
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 12)"
    echo "RUSTFS_ACCESS_KEY=vervedeck"
    echo "RUSTFS_SECRET_KEY=$(openssl rand -hex 16)"
    echo "S3_ENDPOINT=http://rustfs:9000"
    echo "S3_BUCKET=vervedeck-datasets"
    echo "HF_ENDPOINT=https://hf-mirror.com"
    echo "ARGILLA_API_KEY=$(openssl rand -hex 16)"
    echo "GRAFANA_PASSWORD=$(rand_alnum 12)"
    echo "AM_WEBHOOK_TOKEN=$(openssl rand -hex 16)"
    echo "GRAFANA_ANONYMOUS=true"
    echo "CORS_ORIGINS=*"
    echo "API_DOCS_ENABLED=true"
  } > .env
  echo "    已生成 .env（强随机密钥，权限 600；admin 初始密码见 .env ADMIN_PASSWORD）"
else
  # 已有 .env：检查高危默认值是否仍未修改
  for kv in "JWT_SECRET=vervedeck-dev-secret-change-me" "JWT_SECRET=please-change-me-to-a-long-random-string" "POSTGRES_PASSWORD=fintune123" "RUSTFS_SECRET_KEY=rustfsadmin"; do
    if grep -q "^${kv}" .env; then
      echo "    ⚠ 警告: .env 中 ${kv%%=*} 仍是默认弱值，公网部署请立即修改！"
    fi
  done
fi
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
ADMIN_PW=$(grep -E '^ADMIN_PASSWORD=' .env 2>/dev/null | cut -d= -f2)
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "================ 部署完成 ================"
echo "  Web 控制台:     https://${IP:-<服务器IP>}（443 强制 HTTPS，自签证书浏览器需信任）"
echo "  对象存储:       仅本机 127.0.0.1:9000/9001（公网部署严禁暴露 S3 端口）"
echo "  初始账号:       admin / ${ADMIN_PW:-${ADMIN_PASSWORD:-admin123}}（首登强制改密）"
echo "  常用命令:       docker compose -f deploy/docker-compose.yaml ps|logs|down"
echo ""
echo "  ---- 公网部署安全加固清单（详见 docs/11-安全加固.md）----"
echo "  [必做] 防火墙只放 80/443（22 建议限源 IP）: ufw allow from <办公IP> to any port 22; ufw allow 80,443; ufw enable"
echo "  [必做] 首登修改 admin 密码（强制）；系统管理→设置 开启登录 IP 白名单（办公出口 CIDR）"
echo "  [建议] SSH 禁 root 直登+密码登录（PermitRootLogin no / PasswordAuthentication no），装 fail2ban"
echo "  [建议] 自签证书替换为正式域名证书（deploy/tls/vervedeck.{crt,key}）"
echo "  [可选] 公网可关 API 文档与 Grafana 匿名: .env 设 API_DOCS_ENABLED=false / GRAFANA_ANONYMOUS=false 后 compose up -d"
echo "=========================================="

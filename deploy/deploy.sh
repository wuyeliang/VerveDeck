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
  # Grafana 大屏 root_url 需含外部访问 host（含非 443 端口），否则其生成的绝对链接指向错地址；
  # 按服务器 IP + nginx 实际监听端口自动生成（git 默认 443 → 不带端口后缀）
  VD_IP=$(hostname -I 2>/dev/null | awk '{print $1}'); VD_IP=${VD_IP:-127.0.0.1}
  VD_PORT=$(grep -oE 'listen [0-9]+ ssl' ../platform/frontend/nginx.conf 2>/dev/null | grep -oE '[0-9]+' | head -1)
  if [ -n "$VD_PORT" ] && [ "$VD_PORT" != "443" ]; then VD_SUFFIX=":$VD_PORT"; else VD_SUFFIX=""; fi
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
    echo "GRAFANA_ROOT_URL=https://${VD_IP}${VD_SUFFIX}/grafana/"
    echo "GRAFANA_LIGHT_ROOT_URL=https://${VD_IP}${VD_SUFFIX}/grafana-light/"
    echo "SCRAPE_BIND_IP=${VD_IP}"
  } > .env
  echo "    已生成 .env（强随机密钥，权限 600；admin 初始密码见 .env ADMIN_PASSWORD）"
else
  # 已有 .env：检查高危默认值是否仍未修改
  for kv in "JWT_SECRET=vervedeck-dev-secret-change-me" "JWT_SECRET=please-change-me-to-a-long-random-string" "POSTGRES_PASSWORD=fintune123" "RUSTFS_SECRET_KEY=rustfsadmin"; do
    if grep -q "^${kv}" .env; then
      echo "    ⚠ 警告: .env 中 ${kv%%=*} 仍是默认弱值，公网部署请立即修改！"
    fi
  done
  # 补齐 exporter 内网监听 IP（集群模式：主节点 Prometheus 经内网 scrape 本节点 9400/9100）
  if ! grep -q '^SCRAPE_BIND_IP=' .env; then
    SB_IP=$(hostname -I 2>/dev/null | awk '{print $1}'); SB_IP=${SB_IP:-127.0.0.1}
    echo "SCRAPE_BIND_IP=${SB_IP}" >> .env
    echo "    已补齐 .env SCRAPE_BIND_IP=${SB_IP}（exporter 监听内网 IP，无内网网卡时为 127.0.0.1）"
  fi
fi
mkdir -p /data/models/hub /data/datasets /data/jobs /data/evals /data/uploads \
         /data/gpu /data/argilla /data/postgres /data/redis /data/opensearch \
         /data/grafana /data/grafana-light /data/prometheus \
         /data/monitoring/rules /data/monitoring/sd /data/monitoring/am-data
# 数据目录属主必须匹配容器运行用户，否则首启 AccessDenied 崩溃（postgres/redis 由镜像
# entrypoint 以 root 自 chown 无需处理；opensearch/argilla/grafana/prometheus 需在此显式 chown）：
chown -R 1000:0      /data/opensearch 2>/dev/null || true      # opensearch
chown -R 1000:1000   /data/argilla    2>/dev/null || true      # argilla-server（uid=1000）
chown -R 472:472     /data/grafana /data/grafana-light 2>/dev/null || true   # grafana（uid=472）
chown -R 65534:65534 /data/prometheus 2>/dev/null || true      # prometheus（nobody=65534）
# alertmanager 配置为单文件挂载：首启必须存在（否则 docker 建成目录致"目录挂到文件"挂载失败）
[ -f /data/monitoring/alertmanager.yml ] || cp monitoring/alertmanager/alertmanager.yml /data/monitoring/alertmanager.yml

# 网关限流默认配置（web 容器绑定挂载；必须在 compose up 前存在，否则 docker 建出空目录致 nginx 起不来）。
# 平台页面（系统管理→设置→网关限流）保存后会覆盖本文件并热加载，此处仅首启默认值
mkdir -p nginx
if [ ! -f nginx/00-zones.conf ]; then
  cat > nginx/00-zones.conf <<'ZONES'
# VerveDeck 网关限流 —— 首启默认值（平台 系统管理→设置→网关限流 可改，保存即热生效）
limit_req_zone $binary_remote_addr zone=login_limit:10m rate=5r/s;
limit_req_zone $binary_remote_addr zone=api_limit:10m rate=30r/s;
limit_req_zone $binary_remote_addr zone=v1_limit:10m rate=60r/s;
limit_conn_zone $binary_remote_addr zone=conn_limit:10m;
limit_conn conn_limit 50;     # 单 IP 并发连接上限
ZONES
fi

# 端口暴露基线：平台指标底座是 Prometheus 栈（dcgm/node-exporter），PCP（pmcd 44321 /
# pmlogger 4330）默认 0.0.0.0 监听且与平台无关 → 公网部署是信息泄露面，停掉并禁用
# （无 PCP 安装时全部 no-op）
systemctl disable --now pmcd.service pcp.service 2>/dev/null || true
[ -x /etc/init.d/pcp ] && /etc/init.d/pcp stop 2>/dev/null || true
pkill -x pmcd 2>/dev/null || true
pkill -x pmlogger 2>/dev/null || true

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
# 全容器状态核对：防止个别服务（grafana/prometheus/argilla 等）crashloop 被静默当成"部署完成"
bad=$(docker compose ps --format '{{.Service}} {{.State}}' 2>/dev/null | awk '$2=="restarting"||$2=="exited"{print $1, $2}') || true
if [ -n "$bad" ]; then
  echo "  ⚠ 以下服务未正常拉起（多为数据目录属主问题，见 [2/5]；可稍后 docker compose restart <服务名> 修复）:"
  echo "$bad" | sed 's/^/      /'
fi
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
echo "  [必做] 核对端口暴露: ss -tlnp —— 仅 web 入口(80/443)与 22 对外，其余应为 127.0.0.1/内网（矩阵见 docs/11-安全加固.md §2）"
echo "  [必做] 首登修改 admin 密码（强制）；系统管理→设置 开启登录 IP 白名单（办公出口 CIDR）"
echo "  [建议] SSH 禁 root 直登+密码登录（PermitRootLogin no / PasswordAuthentication no），装 fail2ban"
echo "  [建议] 自签证书替换为正式域名证书（deploy/tls/vervedeck.{crt,key}）"
echo "  [可选] 公网可关 API 文档与 Grafana 匿名: .env 设 API_DOCS_ENABLED=false / GRAFANA_ANONYMOUS=false 后 compose up -d"
echo "=========================================="

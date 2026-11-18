#!/usr/bin/env bash
# 一键测试运行器（在服务器 /opt/vervedeck 下执行；本机仅单测可跑）
#
# 用法：
#   ./tests/run_tests.sh fast     # 快速套件（backend 容器内，约 20 分钟，含部署生命周期）
#   ./tests/run_tests.sh e2e      # E2E 真实 GPU 全链路（约 6-14 分钟，需无其他任务占卡）
#   ./tests/run_tests.sh all      # fast + e2e（backend 容器内）
#   ./tests/run_tests.sh runner   # 稳健运行器（推荐：有并发活动的环境）——独立容器跑全量，
#                                 # backend 被重建不波及 pytest；按文件 3 次重试
#
# 约定：测试直连 127.0.0.1:3001（与生产同环境）。
# - fast/e2e/all：在 backend 容器内运行（docker exec）。缺点：backend 容器被重建时
#   pytest 进程随之死亡（并行会话迭代部署时常见）。
# - runner：独立容器（host 网络 + --add-host 解析 compose 服务名）挂载 tests/ 运行，
#   backend 重建只造成瞬态不可用（重试兜底），pytest 进程本身不受影响。
# 前置：服务器 git 已与 origin/main 同步（git pull），backend 镜像已构建。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
CONTAINER="${CONTAINER:-fintune-backend-1}"
MODE="${1:-fast}"

sync_tests() {
  # 先清空防嵌套（docker cp 到已存在的 /app/tests 会嵌套出 /app/tests/tests）
  docker exec "$CONTAINER" sh -c "rm -rf /app/tests && mkdir -p /app/tests"
  docker cp "$ROOT/tests/." "$CONTAINER":/app/tests/
}

run_fast() {
  sync_tests
  docker exec "$CONTAINER" sh -c \
    "cd /app/tests && python -m pytest --ignore=test_e2e.py -v --tb=short -p no:cacheprovider"
}

run_e2e() {
  sync_tests
  docker exec "$CONTAINER" sh -c \
    "cd /app/tests && python -m pytest test_e2e.py -v --tb=short -p no:cacheprovider"
}

# ---------- 稳健运行器（独立容器） ----------
# 2026-11-16 #80 第三轮验证：有并行会话反复重建 backend 时，docker exec 方式必死；
# 独立容器 + 按文件重试可完整跑完（全量 752 用例 18 分钟，零文件重试全绿）。
RUNNER_NAME="ft-test-runner"
COMPOSE_NET="${COMPOSE_NET:-fintune_net}"
RUNNER_IMAGE="${RUNNER_IMAGE:-fintune/backend:1.0}"

run_runner() {
  local files=("$@")
  local pg_ip rf_ip
  pg_ip=$(docker inspect fintune-postgres-1 \
    --format "{{(index .NetworkSettings.Networks \"$COMPOSE_NET\").IPAddress}}")
  rf_ip=$(docker inspect fintune-rustfs-1 \
    --format "{{(index .NetworkSettings.Networks \"$COMPOSE_NET\").IPAddress}}")
  # host 网络容器不能挂第二网络 → 用 --add-host 静态解析 compose 服务名。
  # 关键：deploy.sh 会为 DB / S3 生成随机凭证（DB_URL 随机口令、S3 key 非 rustfsadmin），
  # 而 quantize/storage 等用例进程内 import app 直连 DB/S3，硬编码默认值会让它们
  # OperationalError / InvalidAccessKeyId。故从 backend 容器抓取真实运行凭证并透传，
  # 抓不到再回退默认（保持旧部署/纯本地模式可用）。
  local be_env v
  be_env=$(docker exec "$CONTAINER" env 2>/dev/null \
    | grep -E "^(DB_URL|DATA_DIR|S3_ENDPOINT|S3_ACCESS_KEY|S3_SECRET_KEY|S3_BUCKET)=") || true
  v=$(printf '%s\n' "$be_env" | sed -n 's/^DB_URL=//p');        [ -n "$v" ] && DB_URL="$v"
  v=$(printf '%s\n' "$be_env" | sed -n 's/^DATA_DIR=//p');      [ -n "$v" ] && DATA_DIR="$v"
  v=$(printf '%s\n' "$be_env" | sed -n 's/^S3_ENDPOINT=//p');   [ -n "$v" ] && S3_ENDPOINT="$v"
  v=$(printf '%s\n' "$be_env" | sed -n 's/^S3_ACCESS_KEY=//p'); [ -n "$v" ] && S3_ACCESS_KEY="$v"
  v=$(printf '%s\n' "$be_env" | sed -n 's/^S3_SECRET_KEY=//p'); [ -n "$v" ] && S3_SECRET_KEY="$v"
  v=$(printf '%s\n' "$be_env" | sed -n 's/^S3_BUCKET=//p');     [ -n "$v" ] && S3_BUCKET="$v"
  # JuiceFS FUSE 挂载点须显式同路径 bind（/data 的 rprivate 不传播宿主子挂载，
  # 否则 runner 内 /data/juicefs 只是空目录，落盘断言会误判）。宿主无此目录则不挂。
  local jfs_vol=()
  [ -d /data/juicefs ] && jfs_vol=(-v /data/juicefs:/data/juicefs)
  docker rm -f "$RUNNER_NAME" 2>/dev/null
  docker run -d --name "$RUNNER_NAME" --network host \
    --add-host "postgres:$pg_ip" --add-host "rustfs:$rf_ip" \
    -e DB_URL="${DB_URL:-postgresql://fintune:fintune123@postgres:5432/fintune}" \
    -e DATA_DIR="${DATA_DIR:-/data}" \
    -e S3_ENDPOINT="${S3_ENDPOINT:-http://rustfs:9000}" \
    -e S3_ACCESS_KEY="${S3_ACCESS_KEY:-rustfsadmin}" \
    -e S3_SECRET_KEY="${S3_SECRET_KEY:-rustfsadmin}" \
    -e S3_BUCKET="${S3_BUCKET:-vervedeck-datasets}" \
    -v "$ROOT/tests":/tests -v /data:/data "${jfs_vol[@]}" \
    "$RUNNER_IMAGE" bash -c '
      cd /tests
      wait_healthy() {
        for _ in $(seq 1 36); do
          if python -c "
import sys, urllib.request, urllib.error
try:
    urllib.request.urlopen(\"http://127.0.0.1:3001/api/auth/login\", data=b\"{}\", timeout=3)
except urllib.error.HTTPError:
    sys.exit(0)
except Exception:
    sys.exit(1)
"; then return 0; fi
          sleep 5
        done
        return 1
      }
      fail=0
      for f in "$@"; do
        ok=1
        for try in 1 2 3; do
          if [ "$try" -gt 1 ]; then wait_healthy; fi
          echo "==FILE== $f (try $try)"
          if python -m pytest "$f" -v --tb=short -p no:cacheprovider; then ok=0; break; fi
        done
        if [ "$ok" -ne 0 ]; then fail=1; fi
      done
      echo "=== runner done: $([ $fail -eq 0 ] && echo GREEN || echo HAS-FAILURES) ==="
      [ "$fail" -eq 0 ]
      # 注意：bash -c 'script' "$@" 会把第一个参数吞为 $0（不在 "$@" 内），
      # 故必须垫一个占位符 `_` 作 $0，否则第一个测试文件会被静默跳过。
    ' _ "${files[@]}"
  docker logs -f "$RUNNER_NAME" || true
  local rc
  rc=$(docker inspect -f '{{.State.ExitCode}}' "$RUNNER_NAME" 2>/dev/null || echo 1)
  docker rm -f "$RUNNER_NAME" 2>/dev/null
  return "$rc"
}

case "$MODE" in
  fast) run_fast ;;
  e2e)  run_e2e ;;
  all)  run_fast && run_e2e ;;
  runner)
    mapfile -t FAST_FILES < <(cd "$ROOT/tests" && ls test_*.py | grep -v '^test_e2e\.py$')
    run_runner "${FAST_FILES[@]}" test_e2e.py
    ;;
  *) echo "用法: $0 {fast|e2e|all|runner}"; exit 2 ;;
esac

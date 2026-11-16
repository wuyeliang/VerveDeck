#!/usr/bin/env bash
# 一键测试运行器（在服务器 /opt/vervedeck 下执行；本机仅单测可跑）
#
# 用法：
#   ./tests/run_tests.sh fast   # 快速套件（API 集成 + 单测，约 20-30 分钟，含部署生命周期）
#   ./tests/run_tests.sh e2e    # E2E 真实 GPU 全链路（约 14 分钟，需无其他任务占卡）
#   ./tests/run_tests.sh all    # fast + e2e
#
# 约定：测试在 backend 容器内运行（pytest 直连 127.0.0.1:3001，与生产同环境）。
# 前置：服务器 git 已与 origin/main 同步（git pull）。
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

case "$MODE" in
  fast) run_fast ;;
  e2e)  run_e2e ;;
  all)  run_fast && run_e2e ;;
  *) echo "用法: $0 {fast|e2e|all}"; exit 2 ;;
esac

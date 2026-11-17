#!/usr/bin/env bash
# VerveDeck 一键编译脚本（幂等，可重复执行）
#
# 编译平台全部 3 个本地镜像并做冒烟验证：
#   fintune/backend:1.0   FastAPI 后端        (platform/backend)
#   fintune/web:1.0       React 前端+nginx    (platform/frontend，Dockerfile 内 npm build)
#   fintune/mineru:1.0    PDF/文档解析        (images/mineru，含平台 API 封装 app.py)
# 评测引擎已切官方开源 ModelScope 镜像（内置 evalscope，不在本地编译范围；
# 首次评测任务时由 backend 懒加载拉取，images/evalscope 保留为离线自构兜底）
# 数据标注用官方镜像 argilla/argilla-server:v2.8.0（不在本地编译范围，--pull 可预拉取）
#
# 用法:
#   bash build/build.sh                 # 增量编译（已存在的镜像跳过）+ 冒烟验证
#   bash build/build.sh --rebuild       # 强制重建全部本地镜像
#   bash build/build.sh --only backend  # 只编译指定镜像（逗号分隔，可多个：backend,web）
#   bash build/build.sh --pull          # 编译后预拉取全部外部运行时镜像（postgres/redis/监控栈等）
#   bash build/build.sh --no-verify     # 跳过冒烟验证
#
# 说明:
#   - 本脚本只编译，不启动服务；启动见 deploy/deploy.sh（新机）或 docker compose up -d（更新）
#   - 任务引擎镜像（vllm/sglang/llamafactory 官方镜像）不在编译范围——
#     页面上创建训练/推理任务时由 backend 懒加载（app/services/image_ops.py）
set -euo pipefail
cd "$(dirname "$0")/.."   # 项目根目录

COMPOSE=deploy/docker-compose.yaml
ALL_LOCAL="backend web mineru"
REBUILD=0
PULL=0
VERIFY=1
SELECTED="$ALL_LOCAL"

while [ $# -gt 0 ]; do
  case "$1" in
    --rebuild)   REBUILD=1 ;;
    --pull)      PULL=1 ;;
    --no-verify) VERIFY=0 ;;
    --only)
      [ $# -ge 2 ] || { echo "--only 需要参数（如 backend,web）"; exit 1; }
      SELECTED=$(echo "$2" | tr ',' ' ')
      # 校验合法性
      for s in $SELECTED; do
        case " $ALL_LOCAL " in *" $s "*) ;; *) echo "未知镜像: $s（可选: $ALL_LOCAL）"; exit 1;; esac
      done
      shift
      ;;
    -h|--help)   grep '^# ' "$0" | sed 's/^# //'; exit 0 ;;
    *) echo "未知参数: $1（--help 查看用法）"; exit 1 ;;
  esac
  shift
done

# 名称 → 镜像 tag（与 compose 中 image: 一一对应）
tag_of() {
  case "$1" in
    backend)   echo "fintune/backend:1.0" ;;
    web)       echo "fintune/web:1.0" ;;
    mineru)    echo "fintune/mineru:1.0" ;;
  esac
}

echo "==> [1/4] 环境预检"
command -v docker >/dev/null || { echo "缺少 docker"; exit 1; }
docker compose version >/dev/null 2>&1 || { echo "缺少 docker compose v2"; exit 1; }
AVAIL_KB=$(df -k / | awk 'NR==2{print $4}')
AVAIL_GB=$((AVAIL_KB / 1024 / 1024))
if [ "$AVAIL_GB" -lt 30 ]; then
  echo "    警告: 根分区可用空间仅 ${AVAIL_GB}GB，镜像构建（尤其 mineru 含 PyTorch）可能不足"
else
  echo "    docker/compose 就绪，根分区可用 ${AVAIL_GB}GB"
fi

COMPOSE_TARGETS=""
BUILT=""        # 本次实际构建的镜像（供冒烟验证）
SKIPPED=""

for s in $SELECTED; do
  img=$(tag_of "$s")
  COMPOSE_TARGETS="$COMPOSE_TARGETS $s"
  if docker image inspect "$img" >/dev/null 2>&1 && [ "$REBUILD" != "1" ]; then
    echo "==> 跳过 $img（已存在；--rebuild 可强制重建）"
    SKIPPED="$SKIPPED $img"
  else
    BUILT="$BUILT $s"
  fi
done

echo "==> [2/4] 编译镜像"

# 只把本次需要构建的 compose 服务传给 compose build（全部已存在则为空、不构建）
TARGETS_NOW=""
for s in $COMPOSE_TARGETS; do
  if echo " $BUILT " | grep -q " $s "; then TARGETS_NOW="$TARGETS_NOW $s"; fi
done
if [ -n "${TARGETS_NOW// /}" ]; then
  # shellcheck disable=SC2086
  docker compose -f "$COMPOSE" build $TARGETS_NOW
fi

echo "==> [3/4] 冒烟验证"
fail=0
for s in $BUILT; do
  img=$(tag_of "$s")
  case "$s" in
    backend)
      echo "    backend: compileall + import app.main ..."
      if docker run --rm "$img" sh -c "python -m compileall -q app && python -c 'from app.main import app; print(\"    import ok\")'"; then
        :
      else
        echo "    !! backend 冒烟失败（语法/导入错误，镜像已保留供排查）"; fail=1
      fi
      ;;
    web)
      echo "    web: 检查 dist 产物 ..."
      if docker run --rm "$img" sh -c "test -s /usr/share/nginx/html/index.html"; then
        echo "    index.html ok"
      else
        echo "    !! web 冒烟失败（dist 产物缺失）"; fail=1
      fi
      ;;
    *)
      echo "    $s: 构建成功即通过（GPU 镜像不做运行时冒烟）"
      ;;
  esac
done
if [ "$VERIFY" != "1" ]; then
  echo "    （--no-verify 已跳过）"
fi
[ "$fail" = "0" ] || { echo "冒烟验证未通过，中止"; exit 1; }

if [ "$PULL" = "1" ]; then
  echo "==> [4/4] 预拉取外部运行时镜像"
  # 从 compose 解析全部镜像引用，排除 5 个本地构建 tag，缺什么拉什么
  for img in $(docker compose -f "$COMPOSE" config --images); do
    case " fintune/backend:1.0 fintune/web:1.0 fintune/mineru:1.0 " in
      *" $img "*) continue ;;
    esac
    if docker image inspect "$img" >/dev/null 2>&1; then
      echo "    跳过 $img（已存在）"
    else
      echo "    拉取 $img"
      docker pull "$img"
    fi
  done
else
  echo "==> [4/4] 外部运行时镜像未预拉（--pull 可拉取；deploy/deploy.sh 场景由 compose 启动时自动拉取）"
fi

echo ""
echo "================ 编译完成 ================"
docker images --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}" \
  | grep -E "REPOSITORY|fintune/(backend|web|mineru)|argilla/argilla-server|modelscope-repo" || true
echo "=========================================="
echo "下一步（本机部署/更新）:"
echo "  新机器:  cd deploy && sudo bash deploy.sh"
echo "  代码更新: docker compose -f deploy/docker-compose.yaml up -d backend   # 只重建的服务逐个 up -d"

# 一键编译

`build/` 是项目编译相关代码的唯一入口。一条命令编译平台全部本地镜像，并做冒烟验证。
（数据标注 argilla 已切官方镜像 `argilla/argilla-server:v2.8.0`，不在本地编译范围。）

```bash
bash build/build.sh
```

## 1. 编译范围

### 1.1 本地构建镜像（4 个，脚本编译）

| 镜像 | 构建上下文 | 说明 |
|---|---|---|
| `fintune/backend:1.0` | `platform/backend` | FastAPI 后端（Python 3.10 + uvicorn，依赖走阿里云 PyPI 源） |
| `fintune/web:1.0` | `platform/frontend` | React 前端，Dockerfile 内多阶段构建（npm 走 npmmirror 源 → tsc + vite build → nginx:alpine） |
| `fintune/mineru:1.0` | `images/mineru` | PDF/文档解析（compose 服务，运行占 1 GPU）；**内置平台 API 封装 app.py**（POST /parse 契约），不能直接换官方 opendatalab/mineru（API 契约不同且为 CUDA 大镜像） |
| ~~`fintune/evalscope:1.0`~~ | `images/evalscope` | 评测引擎自构兜底（**默认不再编译**）；已切官方开源 ModelScope 镜像（内置 evalscope，backend 懒加载拉取；平台 runner.py 运行时投递）。离线/气隔环境恢复：build-images.sh 取消注释 build 行 + IMAGES_EVAL 覆盖回 fintune/evalscope:1.0 |

镜像 tag 固定为 `1.0`，与 `deploy/docker-compose.yaml` 中 `image:` 一一对应，勿手工改 tag。

### 1.2 外部运行时镜像（10 个，`--pull` 预拉取）

**argilla/argilla-server:v2.8.0**（数据标注，官方镜像）、postgres:16-alpine、redis:7-alpine、
opensearch 2.17.1、rustfs/rustfs、nvidia/dcgm:4.7.1-ubuntu24.04、nvidia/dcgm-exporter:4.6.1-4.8.4、
prom/node-exporter:v1.8.2、prom/prometheus:v2.55.1、prom/alertmanager:v0.27.0、grafana 11.6.2
（清单以 `docker compose config --images` 为准，脚本自动解析、缺什么拉什么）。

> argilla 切换说明：官方镜像与自构版同版本（argilla-server 2.8.0、同 CLI），compose 的
> `command:` 覆盖方式完全兼容。注意官方镜像以 `argilla` 用户（uid=1000）运行，
> 宿主机数据目录须 `chown 1000:1000 /data/argilla`。`images/argilla/` 保留为
> 离线/气隔环境自构兜底（恢复 compose 中注释即可）。

### 1.3 任务引擎镜像（不在编译范围）

`hiyouga/llamafactory:0.9.5`、`vllm/vllm-openai`、`lmsysorg/sglang` —— 官方预构建大镜像，
页面上创建训练/推理任务时由 backend 检测缺失再自动拉取（懒加载，`backend/app/services/image_ops.py`）。
离线/气隔环境可用 `images/llama-factory` 自构兜底。

## 2. 用法

```bash
bash build/build.sh                 # 增量编译：已存在的镜像跳过，缺的构建 + 冒烟验证
bash build/build.sh --rebuild       # 强制重建全部本地镜像
bash build/build.sh --only backend  # 只编译指定镜像（逗号分隔多个：--only backend,web）
bash build/build.sh --pull          # 编译后再预拉取全部外部运行时镜像（离线机提前备料用）
bash build/build.sh --no-verify     # 跳过冒烟验证（只构建）
bash build/build.sh --help          # 帮助
```

选项可组合，如 `bash build/build.sh --rebuild --only backend`（强制重建后端）。

## 3. 脚本做了什么

1. **环境预检**：docker / compose v2 / 根分区剩余空间（<30GB 告警）
2. **编译**：4 个 compose 镜像走 `docker compose build`（并行），evalscope 走 `docker build`（后台并行）；已存在且未 `--rebuild` 的跳过
3. **冒烟验证**（仅针对本次实际构建的镜像）：
   - backend：`python -m compileall -q app`（全量语法编译）+ `from app.main import app`（导入冒烟，可抓依赖/拼写错误）
   - web：确认 `/usr/share/nginx/html/index.html` 存在（dist 产物非空）
   - 其余 GPU 镜像：构建成功即通过（不做运行时冒烟，避免无 GPU 机器误报）
4. **报告**：5 个镜像的 tag/大小表 + 下一步提示

任一步失败立即中止（`set -euo pipefail`），构建失败会保留失败镜像现场供排查。

## 4. 典型流程

### 4.1 全新机器（完整部署 = 编译 + 启动）

```bash
cd /opt/vervedeck
bash build/build.sh --pull          # 编译全部 + 备齐外部镜像（离线机推荐先跑）
cd deploy && sudo bash deploy.sh    # 部署（内部也会调 build-images.sh，已构建的会跳过）
```

> `deploy/deploy.sh` 面向全新机器：环境预检 → 生成 .env/证书/数据目录 → 构建 → `compose up -d` → 健康等待。
> 完整前置条件与验证步骤见 [docs/02-一键部署.md](../docs/02-一键部署.md)。

### 4.2 代码改动后更新（本机或服务器）

```bash
bash build/build.sh --only backend          # 只重建改动的镜像（最常用）
docker compose -f deploy/docker-compose.yaml up -d backend   # 用新镜像替换运行容器
```

改前端就 `--only web`，改 compose 环境变量不需要重新编译（`up -d` 即可生效）。
`up -d` 只会替换镜像有变化的服务，数据卷与 `/data` 不受影响。

### 4.3 远程服务器（git 同步流）

```bash
# 开发机
git push origin main
# 服务器
cd /opt/vervedeck && git pull
bash build/build.sh --only backend
docker compose -f deploy/docker-compose.yaml up -d backend
```

## 5. 常见问题

| 现象 | 处理 |
|---|---|
| `npm install` / `pip install` 慢或超时 | Dockerfile 已内置国内源（npmmirror / 阿里云 PyPI）；仍失败时检查构建机出网与 registry mirror 配置 |
| 磁盘不足（build 阶段 OOM/ENOSPC） | `docker system df` 查占用，`docker image prune -f` / `docker builder prune -f` 清缓存；mineru/evalscope 单镜像构建峰值可达 20GB+ |
| 改了代码但镜像没变 | 确认走了 `--only <服务>` 且该服务在 BUILT 列表（非「跳过」）；或 `--rebuild` 强制 |
| 只想重建单个镜像的依赖层 | 直接 `docker build --no-cache -t fintune/backend:1.0 platform/backend` |
| 前端产物是旧的 | web 镜像构建上下文是 `platform/frontend` 整个目录，确保本地已无未保存的 dev server 产物；必要时 `--rebuild` |
| compose 版本过旧报错 | 需要 docker compose v2 插件（`docker compose version` 验证）；参考 docs/02-一键部署.md §1.1 安装 |

## 6. 相关文件

| 文件 | 职责 |
|---|---|
| `build/build.sh` | 本目录：一键编译入口（本文档的主角） |
| `deploy/docker-compose.yaml` | 服务栈定义 + 4 个镜像的 build 上下文 |
| `deploy/deploy.sh` | 全新机器一键部署（编译 + 启动 + 健康检查） |
| `deploy/build-images.sh` | 部署脚本调用的辅助镜像构建（evalscope + 拉取 dcgm/argilla 官方镜像），保留供 deploy.sh 复用 |
| `tests/run_tests.sh` | 编译/部署完成后的测试入口（API + 单测） |

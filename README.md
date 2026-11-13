# VerveDeck · 大模型微调训练平台

**Web 全量重写 + 全栈容器化**的大模型微调训练平台：数据工程 → 微调训练 → 评测 → 推理发布 全链路闭环。

> 📄 [简体中文](#中文) ｜ [English](#english)

---

## 中文

### 简介

VerveDeck 是一个开箱即用的一体化 LLM 微调训练平台：在单机/多 GPU 节点上完成从原始文档到可上线推理服务的完整链路。核心特点：

- **全栈容器化**：FastAPI 后端 + React/Ant Design 前端 + nginx 网关，任务级容器隔离（训练/推理/评测各自独立容器）。
- **七大模块**：数据工程、模型资产仓库、分布式微调（LLaMA-Factory）、自动化评测（EvalScope）、高性能推理（vLLM/SGLang）、算力调度运维、安全权限治理。
- **一键部署**：`bash deploy.sh` 完成环境预检 → 配置生成 → 6 个任务镜像构建 → 全栈启动 → 健康检查。
- **国际化**：Web 界面支持 **简体中文、繁體中文、日本語、English、Français、Español** 6 种语言（右上角用户菜单切换，antd/dayjs 组件语言同步联动）。
- **数据集对象存储**：数据集版本主存储为 rustfs（S3 兼容），本地为处理缓存，未命中自动回源；`S3_ENDPOINT` 置空即降级纯本地。

### 功能总览（7 大模块）

| 模块 | 能力 | 技术 |
|---|---|---|
| 1 数据工程 | MinerU 多源文档解析、清洗脱敏（手机/身份证/邮箱/银行卡+自定义正则+去重）、QA/CoT 合成数据、Argilla 多人协同标注、DVC 数据集版本控制 | MinerU + Argilla + DVC |
| 2 模型资产仓库 | 基座模型统一纳管（HF 自动下载）、LoRA/合并模型自动归档、完整血缘追踪（基座→数据→任务→产物→子模型） | 自研 registry + 磁盘 /data/models |
| 3 分布式微调 | CPT/SFT/DPO/合并导出全链路、LoRA/Full、DeepSpeed ZeRO-2/3、可视化超参向导（**94 参数 × 10 组**，对齐 LLaMA-Factory WebUI 全量）、实时 Loss/LR 曲线、断点续训、取消 | LLaMA-Factory |
| 4 自动化评测 | 通用榜单（EvalScope：gsm8k/ceval/arc/cmmlu/human_eval/mbpp）、业务自定义测试集、规则匹配（EM/Contains/F1/ROUGE-L）、LLM-as-a-Judge、微调前后对比图 | EvalScope |
| 5 高性能推理 | vLLM / SGLang 双引擎、量化（awq/gptq/int8）、多 LoRA 动态热加载（运行期 /v1/loras）、OpenAI 兼容 API 发布（代理+鉴权+护栏）、副本弹性扩缩容 | vLLM + SGLang |
| 6 算力调度运维 | GPU 节点池化、多任务排队 + 优先级调度（1-9）、利用率/显存/功耗/温度监控、节点心跳 60s 超时故障自愈（离线判定+任务收敛+可续训） | 自研调度器 + nvidia-smi agent |
| 7 安全权限治理 | 多租户 + 项目空间隔离、RBAC（super_admin/tenant_admin/developer/viewer）、全量写操作审计日志、推理输入/输出内容安全护栏（正则规则库，可全局/项目级） | 自研中间件 |

### 架构约定

- **任务容器隔离**：训练 / 推理 / 评测 三大类任务，每个任务新起一个独立容器；容器主进程为 pause 占位（sleep infinity），backend 通过 docker exec 与任务容器通信（拉起任务、tail 日志、执行探测命令），GPU 设备与内存/CPU 上限按任务独立绑定，资源隔离。
- 数据类任务（解析/清洗/合成）不新起容器：解析走常驻 MinerU 服务；清洗/合成在 backend 线程内执行。
- 所有容器共享宿主机 `/data` 数据卷与 compose 网络；OpenAI 兼容 API 经 backend 反向代理发布（`/v1/<部署名>/...`）。
- **数据集对象存储**：数据集版本文件主存储为 rustfs（S3 API，桶 `vervedeck-datasets`，backend 启动自动建桶），本地 `/data/datasets` 为处理缓存；训练/评测/解析读版本时本地未命中自动从 S3 回源。`S3_ENDPOINT` 置空即无感降级纯本地。
- Argilla 2.x 依赖链（已适配）：必须外接搜索引擎（平台内置单节点 OpenSearch，`DISABLE_SECURITY_PLUGIN=true`）；配置走 `ARGILLA_` 前缀环境变量（`ARGILLA_DATABASE_URL`/`ARGILLA_SEARCH_ENGINE`/`ARGILLA_REDIS_URL`）；API 认证为数据库级用户 API key（compose 启动命令自动建 `fintune` 服务账号）；Web UI 经 nginx 挂在 `/argilla/` 前缀。

### 文档索引

| 文档 | 内容 |
|---|---|
| [docs/01-架构设计.md](docs/01-架构设计.md) | 系统总览、pause 任务容器机制、调度器、数据目录、镜像清单 |
| [docs/02-一键部署.md](docs/02-一键部署.md) | 前置依赖、deploy.sh 一键部署、服务栈与环境变量、对象存储、验证清单、运维命令 |
| [docs/03-数据集与对象存储.md](docs/03-数据集与对象存储.md) | 数据集数据模型、rustfs(S3) 读/写/删路径与降级、key 布局、生产管线 |
| [docs/04-训练参数全表.md](docs/04-训练参数全表.md) | 训练参数 94 项 × 10 组（对齐 LLaMA-Factory 0.9.3 webui）+ 模板 |
| [docs/05-API参考.md](docs/05-API参考.md) | 全模块 REST API 路由表 |
| [docs/06-测试体系.md](docs/06-测试体系.md) | 双层测试（单元/API 套件 + E2E 10 用例）、运行方式、踩坑约定 |
| [docs/07-运维手册.md](docs/07-运维手册.md) | 日常命令、故障排查、备份迁移、容量清理、安全基线 |

### 目录结构

```
deploy/          docker-compose.yaml、.env.example、deploy.sh（一键部署）、build-images.sh、setup-a3.sh
images/          llama-factory / vllm / sglang / evalscope / mineru / argilla 镜像
docs/            完整文档（架构/部署/数据与存储/训练参数/API/测试/运维）
platform/backend FastAPI 平台服务（认证/RBAC/审计/调度器/七大模块 API）
platform/frontend React + Ant Design Web（i18next 6 语言）
```

### 快速部署（Ubuntu 24.04 + NVIDIA GPU）

```bash
# 1. 上传代码（本仓库）到服务器
scp -r ./ root@<server>:/opt/vervedeck

# 2. 服务器基础依赖（Docker + NVIDIA 驱动 + nvidia-container-toolkit，仅全新机器需要）
#    逐条安装脚本见 docs/02-一键部署.md「前置依赖」；装完验证：
nvidia-smi   # 若不可用（首次装驱动），reboot 后再验证

# 3. 一键部署（生成 .env 与 /data 目录 → 构建 6 个任务镜像 → 启动全栈 → 健康检查）
cd /opt/vervedeck/deploy
bash deploy.sh
```

deploy.sh 五步：环境预检（nvidia-smi 缺失仅警告）→ 生成 `.env`（基于 `.env.example`）+ `/data` 目录 → `build-images.sh` 构建 6 个任务镜像（已存在跳过，`REBUILD=1` 强制重建）→ `docker compose up -d` → 轮询 backend `/health` 与 rustfs 健康，打印入口与默认账号。

首次运行最耗时的是任务镜像构建（torch 下载大）；重复执行幂等，已存在的镜像跳过。

手工分步流程、完整环境变量参考、部署验证清单见 [docs/02-一键部署.md](docs/02-一键部署.md)。

### 验收冒烟流程

1. 浏览器打开 `http://<server>:3000`，用 `admin / admin123` 登录。
2. 系统管理 → 创建用户/租户/项目（RBAC），创建 API Key。
3. 模型仓库 → 纳管基座模型 `Qwen/Qwen2.5-0.5B-Instruct`（自动下载，状态 pending_download→ready）。
4. 数据中心 → 新建数据集 → 上传 JSONL 生成 v1 → DVC 提交；或上传 PDF 走 MinerU 解析；对版本做清洗脱敏；部署一个模型后用其合成 QA 数据。数据集版本主存储为 rustfs 对象存储（`vervedeck-datasets` 桶，控制台 `:9001`），本地 `/data/datasets` 为处理缓存，未命中自动回源；系统总览 API 返回存储健康（enabled/ok/bucket）。
5. 推理部署 → 部署 0.5B 模型（vLLM，约 1-2 分钟加载）→ Playground 对话；创建 API Key 后用 curl 调 OpenAI 接口。
6. 训练中心 → 新建 SFT 任务（LoRA，选 v1 数据）→ 排队 → GPU 调度运行 → 实时 Loss 曲线 → 完成后自动注册 LoRA 产物（模型仓库可见血缘）。
7. 评测中心 → 对基座与 LoRA 分别跑自定义测试集（rule/judge）+ gsm8k 榜单（limit 20）→ 勾选两项做前后对比图。
8. 集群运维 → GPU 利用率曲线、任务队列调优先级；集群管理页可加护栏规则（如输入含"内部机密"→451 拦截）。
9. 审计日志页可见全部写操作记录。

### 端到端测试（10 用例）

覆盖七大模块：认证/租户/RBAC/模型仓库血缘/数据集 DVC+Argilla/推理 OpenAI API/SFT LoRA 训练+产物血缘/规则评测/LoRA 热加载/护栏+审计。单元/API 快速套件（13 文件，含 S3 对象存储读写回源/删桶验证与 94 参数 build_config 全量断言）与 E2E 的关系、运行方式、踩坑约定见 [docs/06-测试体系.md](docs/06-测试体系.md)。

```bash
# 1) 测试不随镜像发布，先拷入 backend 容器（镜像重建后需重拷）
docker exec fintune-backend-1 mkdir -p /app/tests
docker cp /opt/vervedeck/platform/backend/tests/. fintune-backend-1:/app/tests/

# 2) backend 容器内运行（直连本进程 3001；pytest 已内置镜像）
docker exec fintune-backend-1 bash -lc \
  "cd /app/tests && python -m pytest test_e2e.py -v --tb=short -p no:cacheprovider"
```

前置：常驻栈已启动、默认项目(16) 已纳管基座 `qwen25-05b`（资产 id=1, ready）、推理部署 id=1（`qwen25-05b-base`）。
TC07 会先停推理部署释放 GPU（单卡互斥），训练真实执行（0.5B LoRA，约 1 分钟）；TC06/08 各触发一次 vLLM 冷启动（KVM 虚机实测 5-8 分钟，就绪等待窗口 900s）。

### 任务镜像依赖要点（踩坑记录）

- **vllm 镜像**：`vllm==0.15.1`（首个提供运行期 LoRA 热加载 API 的版本线：`POST /v1/load_lora_adapter` / `POST /v1/unload_lora_adapter`；实测 0.8.5/0.9.2/0.10-0.13 均无该 API，热加载全 404）+ `transformers>=4.56,<5`（0.15.1 声明范围，4.57.6 可用；transformers 5.x 移除 `all_special_tokens_extended` 不可用；注意 0.9.2 反而必须 pin 4.53.2——4.57 的 aimv2 注册与其 Ovis 配置冲突，导入即崩）+ 系统包 `gcc g++`（V1 引擎 torch._inductor 编译 CUDA kernel 必需，缺 CC 报 `Failed to find C compiler`）。依赖 torch==2.9.1 硬 pin。
- **LoRA 热加载开关**：vLLM 0.15.x 的动态 LoRA API 默认关闭（官方标注"仅本地开发"），平台在推理容器 env 显式设 `VLLM_ALLOW_RUNTIME_LORA_UPDATING=1`（inference_service.start_replica），否则 /v1/load_lora_adapter 返回 404。
- **evalscope 镜像**：同样 pin `transformers>=4.49,<5`。
- **CPU 限额必须用 `cpu_quota`/`cpu_period`**：实测 Docker 29.9.3 daemon 对 docker-py 7.2.0 的 `nano_cpus` 参数误限流为固定 `cpu.max=1000 100000`（0.01 核，容器 import 极慢），`cpu_quota=int(cpus*100000)`+`cpu_period=100000` 行为正常（与 CLI `--cpus` 等价）。
- **gpu-agent CSV**：nvidia-smi `--format=csv,nounits`（默认带表头，`noheader` 与 DictReader 解析不匹配；`csv,header` 不被 nvidia-smi 支持）；输出键值带前导空格（`" name"`、`" 1"`），解析必须 strip。
- **部署状态机**：start 端点后台线程 `wait_ready(1200)` 翻转 creating→running/failed（模型加载超时 20 分钟判 failed）；调度器兜底对账（容器丢失→failed，creating 且端点 200→running）；启动/停止换发 launch token 作废旧后台线程，防止其超时回写覆盖新状态；状态翻正时同步清空 error 字段，避免 DB 显示陈旧错误。
- **OpenAI 代理**：对外 `/v1/<部署名>/<rest>`，转发目标必须补 `/v1/` 前缀（vLLM 的 OpenAI 路由在 `/v1/` 下，直接转发 `/models` 会 404）。**httpx 客户端生命周期**：endpoint 的 `finally` 在返回 StreamingResponse 之后、body 被消费之前执行，若在那里 `aclose()` 会导致流式响应空 body（200 但无内容）——非流式请求须在 endpoint 内读完整段再关 client，流式响应须由 generator 的 finally 负责关闭。
- **任务命令拼装**：`["bash", "-lc", "<脚本>"]` 之后再 append 的 argv 元素只是 bash 的位置参数（$0/$1），不会被执行——vLLM 的所有 CLI 参数必须拼进脚本串（曾导致 `--enable-lora` 静默失效，LoRA 热加载报 400）。
- **评测任务链**：task.json 由 backend 写到宿主机 `/data/evals/<id>/`（容器经挂载可见），镜像只内置 `/workspace/runner.py`，exec 命令负责 `cp /data/evals/<id>/task.json /workspace/task.json` 后再跑 runner；ORM 会话要点：传入 service 的 ORM 对象挂在调用方会话上，须 `db.merge(task)` 绑定本会话才能把 `work_dir` 等字段 commit 落库；`commit()` 会 expire 全部属性（expire_on_commit），session 关闭后再访问 ORM 属性触发 detached refresh 崩溃，故 task.json 必须在 `db.close()` 之前装配。
- **审计中间件**：写操作（POST/PUT/DELETE/PATCH）全量记录 + 401/403 安全事件无论读/写方法都留痕（RBAC 拒绝多为 GET 触发，只记写操作会漏掉 403）。

### 关键端口

| 端口 | 服务 |
|---|---|
| 3000 | Web（nginx，唯一对外入口；/api、/v1 代理到 backend） |
| 3000/argilla/ | Argilla 标注 UI（经 nginx 反代） |
| 3001 | backend API（内网） |
| 9000 | rustfs S3 API（对象存储；backend 存取数据集版本） |
| 9001 | rustfs 控制台（对象存储 Web UI） |

### 国际化说明

- 前端基于 **i18next + react-i18next**，资源文件 `platform/frontend/src/locales/{zh-CN,zh-TW,ja,en,fr,es}.json`，fallback 语言 zh-CN。
- 语言检测顺序：localStorage `lang` → 浏览器 `navigator.language` 前缀匹配 → 默认 zh-CN；切换后立即生效（含 antd 组件文案与 dayjs 日期格式），并持久化到 localStorage。
- 训练超参向导的 94 个参数标签与 10 个参数组标题全部随语言切换翻译（`pg.*` / `pp.*` 键位，后端 spec label 兜底）。
- 新增语言：复制任一 locale JSON 全量翻译 → 在 `src/i18n.ts` 的 `resources` 与 `LANGS` 中注册 → 在 `src/main.tsx` 的 `ANT_LOCALES`/`DAY_LOCALES` 映射中加入 antd/dayjs locale 即可。

### 已知边界

- 单节点 1×3080（20GB）规模验证；多节点扩展时 gpu-agent 与节点注册表已按多节点设计（GpuNode 表），调度准入逻辑无需改动。
- 3080 不支持 FP8；量化走 AWQ/GPTQ/INT8（需对应量化权重）。
- OpenCompass 官方镜像较重，平台评测引擎采用同定位的 EvalScope（榜单 + 自定义 + Judge 全覆盖）。

---

## English

### Overview

VerveDeck is a ready-to-use, **fully containerized LLM fine-tuning & training platform** that closes the loop from raw documents to a production OpenAI-compatible inference service on single or multi-GPU nodes.

Key highlights:

- **Fully containerized stack**: FastAPI backend + React/Ant Design frontend + nginx gateway, with per-task container isolation (each training / inference / evaluation job runs in its own dedicated container).
- **Seven modules**: data engineering, model asset repository, distributed fine-tuning (LLaMA-Factory), automated evaluation (EvalScope), high-performance inference (vLLM/SGLang), GPU scheduling & ops, and security & governance.
- **One-click deployment**: `bash deploy.sh` performs environment pre-check → config generation → build of 6 task images → full-stack startup → health checks.
- **Internationalization**: the Web UI ships with **Simplified Chinese, Traditional Chinese, Japanese, English, French and Spanish** (switch from the user menu at the top right; antd & dayjs component locales switch in sync).
- **Dataset object storage**: dataset version files live primarily in rustfs (S3-compatible); the local disk is a processing cache with automatic S3 fallback. Setting `S3_ENDPOINT` to empty gracefully degrades to local-only storage.

### Feature Modules (7)

| Module | Capabilities | Technology |
|---|---|---|
| 1 Data Engineering | MinerU multi-source document parsing, cleaning & PII masking (phone/ID/email/bank card + custom regex + dedup), QA/CoT synthetic data, Argilla collaborative annotation, DVC dataset versioning | MinerU + Argilla + DVC |
| 2 Model Asset Repository | Unified base-model management (auto-download from HuggingFace), automatic archiving of LoRA/merged models, full lineage tracking (base → data → job → artifact → derived models) | Custom registry + /data/models |
| 3 Fine-tuning | Full CPT/SFT/DPO/merge-export pipeline, LoRA/Full fine-tuning, DeepSpeed ZeRO-2/3, visual hyperparameter wizard (**94 params × 10 groups**, aligned with the full LLaMA-Factory WebUI), real-time Loss/LR curves, resume-from-checkpoint, cancel | LLaMA-Factory |
| 4 Evaluation | Public benchmarks (EvalScope: gsm8k/ceval/arc/cmmlu/human_eval/mbpp), custom business test sets, rule-based scoring (EM/Contains/F1/ROUGE-L), LLM-as-a-Judge, before/after comparison charts | EvalScope |
| 5 High-perf Inference | vLLM / SGLang dual engines, quantization (awq/gptq/int8), multi-LoRA dynamic hot-loading (runtime /v1/loras), OpenAI-compatible API publishing (proxy + auth + guardrails), elastic replica scaling | vLLM + SGLang |
| 6 GPU Scheduling & Ops | GPU node pooling, multi-task queue with priority scheduling (1-9), utilization/VRAM/power/temperature monitoring, node heartbeat (60s) fault self-healing (offline detection + job convergence + resumable) | Custom scheduler + nvidia-smi agent |
| 7 Security & Governance | Multi-tenant + project isolation, RBAC (super_admin/tenant_admin/developer/viewer), full audit log of write operations, input/output content-safety guardrails (regex rule base, global/project level) | Custom middleware |

### Architecture Notes

- **Task container isolation**: each training / inference / evaluation job starts a fresh container whose main process is a `pause` placeholder (`sleep infinity`); the backend talks to job containers via `docker exec` (launch jobs, tail logs, run probes). GPU devices and CPU/memory limits are bound per job for strict resource isolation.
- Data tasks (parse/clean/synth) do not spawn containers: parsing goes through the persistent MinerU service; cleaning/synthesis run in backend threads.
- All containers share the host `/data` volume and the compose network; the OpenAI-compatible API is published through the backend reverse proxy (`/v1/<deployment>/...`).
- **Dataset object storage**: dataset version files are stored primarily in rustfs (S3 API, bucket `vervedeck-datasets`, auto-created on backend startup); local `/data/datasets` is the processing cache. Training/evaluation/parsing transparently refetch from S3 on local cache miss. Setting `S3_ENDPOINT` to empty degrades to local-only.
- Argilla 2.x dependency chain (adapted): an external search engine is required (the platform bundles a single-node OpenSearch with `DISABLE_SECURITY_PLUGIN=true`); configuration uses `ARGILLA_`-prefixed env vars (`ARGILLA_DATABASE_URL`/`ARGILLA_SEARCH_ENGINE`/`ARGILLA_REDIS_URL`); API auth uses a database-level user API key (the compose entrypoint auto-creates the `fintune` service account); the Web UI is served under `/argilla/` via nginx.

### Documentation

| Doc | Content |
|---|---|
| [docs/01-架构设计.md](docs/01-架构设计.md) | System overview, pause task-container mechanism, scheduler, data directories, image inventory (Chinese) |
| [docs/02-一键部署.md](docs/02-一键部署.md) | Prerequisites, one-click deploy.sh, service stack & env vars, object storage, verification checklist, ops commands (Chinese) |
| [docs/03-数据集与对象存储.md](docs/03-数据集与对象存储.md) | Dataset data model, rustfs(S3) read/write/delete paths & fallback, key layout, production pipeline (Chinese) |
| [docs/04-训练参数全表.md](docs/04-训练参数全表.md) | 94 training params × 10 groups (aligned with LLaMA-Factory 0.9.3 webui) + templates (Chinese) |
| [docs/05-API参考.md](docs/05-API参考.md) | REST API route table for all modules (Chinese) |
| [docs/06-测试体系.md](docs/06-测试体系.md) | Two-tier testing (unit/API suite + 10 E2E cases), how to run, pitfalls (Chinese) |
| [docs/07-运维手册.md](docs/07-运维手册.md) | Daily commands, troubleshooting, backup/migration, capacity cleanup, security baseline (Chinese) |

### Directory Layout

```
deploy/          docker-compose.yaml, .env.example, deploy.sh (one-click), build-images.sh, setup-a3.sh
images/          llama-factory / vllm / sglang / evalscope / mineru / argilla task images
docs/            Full documentation (architecture / deploy / data & storage / params / API / tests / ops)
platform/backend FastAPI platform service (auth / RBAC / audit / scheduler / 7-module APIs)
platform/frontend React + Ant Design web (i18next, 6 languages)
```

### Quick Start (Ubuntu 24.04 + NVIDIA GPU)

```bash
# 1. Upload this repository to the server
scp -r ./ root@<server>:/opt/vervedeck

# 2. Base server dependencies (Docker + NVIDIA driver + nvidia-container-toolkit, fresh machines only)
#    Full step-by-step script: docs/02-一键部署.md "Prerequisites". Verify with:
nvidia-smi   # if unavailable after a first-time driver install, reboot and re-verify

# 3. One-click deploy (generate .env & /data dirs → build 6 task images → start stack → health checks)
cd /opt/vervedeck/deploy
bash deploy.sh
```

deploy.sh runs five steps: environment pre-check (missing nvidia-smi is a warning only) → generate `.env` (from `.env.example`) + `/data` directories → `build-images.sh` builds the 6 task images (skipped if present; force with `REBUILD=1`) → `docker compose up -d` → poll backend `/health` and rustfs health, then print the entry URL and default credentials.

The first run is dominated by task-image build time (large torch downloads); reruns are idempotent and skip existing images.

See [docs/02-一键部署.md](docs/02-一键部署.md) (Chinese) for the manual step-by-step flow, full env-var reference, and the deployment verification checklist.

### Acceptance Smoke Flow

1. Open `http://<server>:3000` in a browser, log in with `admin / admin123`.
2. Admin → create user/tenant/project (RBAC), create an API Key.
3. Model Repo → register base model `Qwen/Qwen2.5-0.5B-Instruct` (auto-download; status pending_download → ready).
4. Data Center → create dataset → upload JSONL to make v1 → DVC commit; or upload a PDF for MinerU parsing; clean/mask a version; after deploying a model, use it to synthesize QA data. Dataset versions are stored in rustfs (bucket `vervedeck-datasets`, console on :9001) with local `/data/datasets` as cache; the dashboard API reports storage health (enabled/ok/bucket).
5. Inference → deploy the 0.5B model (vLLM, ~1-2 min load) → chat in the Playground; then call the OpenAI endpoint with curl using an API Key.
6. Training → create an SFT job (LoRA, using v1 data) → queued → scheduled to GPU → live Loss curve → on completion the LoRA artifact is auto-registered (lineage visible in Model Repo).
7. Evaluation → run the custom test set (rule/judge) and the gsm8k benchmark (limit 20) against base and LoRA → select both results for the before/after comparison chart.
8. Cluster Ops → GPU utilization curves, adjust queue priorities; add guardrail rules (e.g. input containing a secret keyword → HTTP 451 blocked).
9. The Audit Log page shows every write operation.

### End-to-End Tests (10 cases)

Cover all seven modules: auth/tenant/RBAC/model lineage/dataset DVC+Argilla/inference OpenAI API/SFT LoRA training + artifact lineage/rule-based evaluation/LoRA hot-loading/guardrails + audit. The relationship between the fast unit/API suite (13 files, including S3 object-storage read/write/refetch/bucket-deletion checks and full 94-param build_config assertions) and the E2E suite, plus run instructions and pitfalls, is documented in [docs/06-测试体系.md](docs/06-测试体系.md) (Chinese).

```bash
# 1) Tests are not shipped in the image; copy them into the backend container (re-copy after image rebuilds)
docker exec fintune-backend-1 mkdir -p /app/tests
docker cp /opt/vervedeck/platform/backend/tests/. fintune-backend-1:/app/tests/

# 2) Run inside the backend container (direct to 3001; pytest is baked into the image)
docker exec fintune-backend-1 bash -lc \
  "cd /app/tests && python -m pytest test_e2e.py -v --tb=short -p no:cacheprovider"
```

Prerequisites: the resident stack is up, default project (16) has the base `qwen25-05b` registered (asset id=1, ready), inference deployment id=1 (`qwen25-05b-base`).
TC07 first stops the inference deployment to free the GPU (single-card mutual exclusion) and really trains (0.5B LoRA, ~1 min); TC06/08 each trigger one vLLM cold start (measured 5-8 min on a KVM VM; 900s readiness window).

### Task Image Dependency Notes (Pitfall Log)

- **vllm image**: `vllm==0.15.1` (first release line with the runtime LoRA hot-load API: `POST /v1/load_lora_adapter` / `POST /v1/unload_lora_adapter`; 0.8.5/0.9.2/0.10-0.13 lack it — hot-load returns 404) + `transformers>=4.56,<5` (4.57.6 works; transformers 5.x removed `all_special_tokens_extended` and is unusable; note 0.9.2 conversely requires pinning 4.53.2 — 4.57's aimv2 registration conflicts with its Ovis config and crashes on import) + system packages `gcc g++` (required for the V1 engine's torch._inductor CUDA kernel compilation; without a C compiler you get `Failed to find C compiler`). Hard-pins torch==2.9.1.
- **LoRA hot-load switch**: vLLM 0.15.x disables the dynamic LoRA API by default (marked "local development only"); the platform sets `VLLM_ALLOW_RUNTIME_LORA_UPDATING=1` in the inference container env (inference_service.start_replica), otherwise /v1/load_lora_adapter returns 404.
- **evalscope image**: likewise pins `transformers>=4.49,<5`.
- **CPU limits must use `cpu_quota`/`cpu_period`**: measured on Docker 29.9.3 daemon, docker-py 7.2.0's `nano_cpus` parameter mis-limits to a fixed `cpu.max=1000 100000` (0.01 cores, extremely slow container imports); `cpu_quota=int(cpus*100000)` + `cpu_period=100000` behaves correctly (equivalent to CLI `--cpus`).
- **gpu-agent CSV**: nvidia-smi `--format=csv,nounits` (default has a header; `noheader` breaks DictReader parsing; `csv,header` is not supported by nvidia-smi); output key/values carry leading spaces (`" name"`, `" 1"`), parsing must strip.
- **Deployment state machine**: the start endpoint runs a background `wait_ready(1200)` thread flipping creating→running/failed (model load timeout of 20 min ⇒ failed); the scheduler performs reconciliation as a safety net (lost container → failed; creating with endpoint 200 → running); start/stop rotate a launch token to invalidate stale background threads so their timeout writes cannot overwrite newer state; when state goes healthy the stale `error` field is cleared so the DB doesn't show outdated errors.
- **OpenAI proxy**: external path is `/v1/<deployment>/<rest>`; the upstream target must add the `/v1/` prefix (vLLM's OpenAI routes live under `/v1/`; forwarding `/models` directly gives 404). **httpx client lifecycle**: the endpoint's `finally` runs after StreamingResponse is returned but before the body is consumed — calling `aclose()` there yields an empty streamed body (200 but no content). Non-streaming requests must read fully inside the endpoint before closing; streaming responses must close the client in the generator's finally.
- **Task command assembly**: argv elements appended after `["bash", "-lc", "<script>"]` are merely bash positional parameters ($0/$1) and are NOT executed — every vLLM CLI argument must be baked into the script string (this once made `--enable-lora` silently fail, and LoRA hot-load returned 400).
- **Evaluation task chain**: the backend writes task.json to the host `/data/evals/<id>/` (visible in the container via mount); the image only ships `/workspace/runner.py`, and the exec command first `cp /data/evals/<id>/task.json /workspace/task.json` before running the runner. ORM session notes: ORM objects passed into services are attached to the caller's session — `db.merge(task)` is required to bind them to this session so fields like `work_dir` commit correctly; `commit()` expires all attributes (expire_on_commit), and touching ORM attributes after `session.close()` triggers a detached-refresh crash — so task.json must be assembled before `db.close()`.
- **Audit middleware**: all write operations (POST/PUT/DELETE/PATCH) are recorded, and 401/403 security events are logged regardless of read/write method (RBAC denials are usually triggered by GET — recording writes only would miss 403s).

### Key Ports

| Port | Service |
|---|---|
| 3000 | Web (nginx, the only public entry; /api and /v1 proxied to backend) |
| 3000/argilla/ | Argilla annotation UI (via nginx reverse proxy) |
| 3001 | backend API (internal) |
| 9000 | rustfs S3 API (object storage; backend reads/writes dataset versions) |
| 9001 | rustfs console (object storage Web UI) |

### Internationalization (i18n)

- Frontend is built on **i18next + react-i18next**; resource files live in `platform/frontend/src/locales/{zh-CN,zh-TW,ja,en,fr,es}.json` with fallback language zh-CN.
- Language detection order: localStorage `lang` → browser `navigator.language` prefix match → default zh-CN. Switching applies immediately (including antd component strings and dayjs date formats) and persists to localStorage.
- All 94 hyperparameter labels and 10 group titles in the training wizard are translated per language (`pg.*` / `pp.*` keys; backend spec labels serve as fallback).
- To add a language: copy any locale JSON and translate → register it in `resources` and `LANGS` in `src/i18n.ts` → add the antd/dayjs locale mapping in `src/main.tsx` (`ANT_LOCALES`/`DAY_LOCALES`).

### Known Boundaries

- Validated at single-node 1×3080 (20GB) scale; for multi-node scale-out the gpu-agent and node registry are already multi-node by design (GpuNode table) and the scheduler admission logic needs no changes.
- The 3080 does not support FP8; quantization goes through AWQ/GPTQ/INT8 (matching quantized weights required).
- The official OpenCompass image is heavy, so the platform's evaluation engine uses EvalScope at the same positioning (benchmarks + custom sets + Judge, fully covered).

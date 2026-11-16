# 测试体系（一级目录 tests/）

2026-11-16 起测试套件统一收敛到仓库一级目录 `tests/`（原 `platform/backend/tests/`，
#80 迁移）。旧路径保留符号链接兼容历史命令。

## 目录结构

```
tests/
├── README.md            本文件
├── run_tests.sh         一键运行器（服务器）：runner / fast / e2e / all
├── conftest.py          共享 fixture（SUF 后缀、租户/项目上下文、poll/retry_call）
├── test_e2e.py          E2E 真实 GPU 全链路（数据集→清洗→SFT→合并→部署→推理→评测）
├── test_unit_*.py       纯单测（ORM/配置构建/命令生成，无外部依赖，任意 Python 环境可跑）
├── test_unit_training_batch.py  训练任务批量删除单测（#97 补漏）
├── test_api_auth.py     登录/JWT/改密
├── test_api_rbac.py     RBAC 角色矩阵（全角色 × 全路由）
├── test_api_admin.py    租户/项目/用户/API Key/护栏/审计/通知
├── test_api_datasets.py 数据集 CRUD/版本/清洗/合成/解析
├── test_api_models.py   模型仓库/血缘/上传/下载状态
├── test_api_training.py 训练任务创建校验/排队/取消/续训/参数面
├── test_api_evals.py    评测生命周期/对比
├── test_api_inference.py 部署管理/OpenAI 兼容代理/LoRA 热加载
├── test_api_deploy_lifecycle.py 部署状态机（唯一一次真实部署切换）
├── test_api_cluster_dashboard.py 节点/队列/优先级/Dashboard
├── test_api_cluster_nodes.py     计算节点纳管 CRUD（#60）+ 系统/推理总览
├── test_api_storage.py  对象存储 S3 集成（无 S3 整组 skip）
├── test_api_quantize.py 量化（模型压缩）33 用例
├── test_api_notify.py   通知通道配置/校验/RBAC
├── test_api_upgrade.py  集群升级 status/check/execute 校验
├── test_api_time.py     集群时间配置（时区 + NTP，不真实 apply）
├── test_api_batch_delete.py  九端点批量删除统一契约（#97/#98）
├── test_api_login_security.py  登录安全配置（#76）
├── test_api_images.py   镜像管理/探测非阻塞回归/自定义镜像
└── test_api_logs.py     日志中心（非 compose 环境整组 skip）
```

## 运行方式

测试运行在 **backend 容器内**（pytest 直连 127.0.0.1:3001，与生产同环境）：

```bash
# 服务器 /opt/vervedeck 下（推荐）
./tests/run_tests.sh runner  # 稳健运行器（并发环境推荐）：独立容器跑全量，
                             # backend 被重建不波及，按文件 3 次重试（约 18 分钟）
./tests/run_tests.sh fast   # 快速套件（backend 容器内，约 20-30 分钟，含部署生命周期真实切换）
./tests/run_tests.sh e2e    # E2E 全链路（约 14 分钟，需无其他任务占卡）
./tests/run_tests.sh all

# 等价的手动命令（fast）
docker exec fintune-backend-1 sh -c "rm -rf /app/tests && mkdir -p /app/tests"
docker cp /opt/vervedeck/tests/. fintune-backend-1:/app/tests/
docker exec fintune-backend-1 sh -c \
  "cd /app/tests && python -m pytest --ignore=test_e2e.py -v --tb=short -p no:cacheprovider"
```

**两种服务器运行方式的区别**（#80 第三轮实证）：

| 方式 | 进程位置 | backend 被重建时 | 适用 |
|---|---|---|---|
| `fast/e2e/all`（docker exec） | backend 容器内 | pytest 随之死亡 | 环境无并发活动 |
| `runner`（独立容器） | host 网络独立容器，挂载 `tests/` + `/data` | 仅瞬态 API 不可用，按文件重试兜底 | 有并行会话/部署活动 |

runner 细节：`--network host` 直连 127.0.0.1:3001；host 网络容器不能挂第二
网络，故用 `--add-host` 静态解析 `postgres`/`rustfs`（compose 服务名，单测文件
进程内建 engine 需要）；S3 环境变量与 backend 一致（storage 用例不降级）；
每个文件最多 3 次尝试，重试前先等待 API 健康（最长 180s）。

本地（无 GPU/无 compose）：仅 `test_unit_*.py` 可跑（`python -m pytest tests/test_unit_*.py`）；
API 测试依赖常驻部署 1（qwen25-05b-base）、Prometheus/DCGM、docker socket、S3 等，
必须在真实环境（如 100.100.33.86）运行。

## 关键约定（踩坑沉淀）

1. **实体唯一后缀 `SUF`**（uuid hex6）：所有测试实体带后缀，套件可重复运行互不干扰；
2. **GPU 常驻部署占用**：默认项目有常驻推理部署（占 1 GPU），快速套件的 GPU 任务
   全部保持 queued（只测排队/取消/优先级），不触发真实训练；真实 GPU 链路只在
   E2E 与部署生命周期文件覆盖；
3. **ORM 过期陷阱**：`db.commit()` 后属性过期（expire_on_commit），跨线程/commit 后
   访问前必须捕获纯值——worker 线程闭包只能捕获纯值；
4. **S3 降级**：`S3_ENDPOINT` 为空时 storage 用例整组 skip；
5. **断言宽松于实现细节**：dashboard/summary 只断言键子集（新增字段不破坏旧断言）；
6. **路由遮蔽**：`GET /api/datasets/tasks` 必须注册在 `GET /{dsid}` 之前（字面量优先）；
7. **docker cp 嵌套坑**：拷入前先 `rm -rf /app/tests`，否则会嵌套出 `/app/tests/tests`；
8. **容器无 pytest 插件**：无 pytest-timeout/pytest-rerunfailures，重试一律用
   conftest 的 `retry_call`（见下）。

## flaky 对策（#80）

全量运行窗口内偶发失败、单跑通过的时序/资源竞争用例，用两种机制隔离：

| 用例 | 根因 | 对策 |
|---|---|---|
| `test_utilization_used_gte_1` | 常驻部署 1 被并发改动（容器重建/升级）拉起重启，creating→running 的数分钟窗口内 used_gpus/running_deployments 归零 | 改轮询等待收敛（poll 300s/10s），不再一次断言 |
| `test_synth_real` | 合成依赖常驻部署的真实 LLM 调用，上游偶发超时/重启致任务 failed | `retry_call` 整段重试 3 次（每次全新任务，20s 退避） |

新增用例若依赖共享常驻资源（部署 1 / 单 GPU / 后台探测），优先用 `poll` +
`retry_call` 而非固定 sleep 或一次断言。

## 变更记录

- **2026-11-16（#80 第三轮）**：终轮全量验收 **752/752 全绿**（runner 模式，
  27 文件零重试，约 18 分钟）；`run_tests.sh` 新增 `runner` 稳健运行器（独立容器
  + 按文件 3 重试，backend 重建不波及）；测试加固：`batch_delete` 部署 1 自愈
  fixture（stopped→start→poll running）、`cluster_nodes` viewer 用例漏参修复 +
  推理总览瞬态轮询；发现并修复 2 个产品缺陷（`cluster/inference` 指标 NaN 致 500、
  `scale` 同副本数 no-op 非幂等致 400，见 08-验收报告 §3.3）；清理历史残留
  （测试节点 ×8、被杀运行遗留的 running 量化任务行）。
- **2026-11-16（#80）**：套件由 `platform/backend/tests/` 迁入一级目录 `tests/`；
  新增 5 个功能测试文件（批量删除 / 节点纳管 / 登录安全 / 镜像 / 日志，共 54 用例）；
  两例 flaky 加轮询/重试对策；修复 3 个批量删除端点 `deleted` 契约不一致（int→id 列表）；
  修复过期代理用例（#96 model 归一化后 404→200+归一名）。

# API 参考

Base URL：`http://<ip>:3000/api/...`（经 web 反代；容器内网为 `:3001`）
认证：`Authorization: Bearer <token>`（`POST /api/auth/login` 获取，默认 72h 有效）

## 认证 /api/auth

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/auth/login | `{username, password}` → `{token, user}` |
| GET | /api/auth/me | 当前用户信息 |
| PUT | /api/auth/password | 修改密码 `{old_password, new_password}` |

## 管理 /api/admin（admin 或项目 owner）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | /api/admin/tenants | 租户列表 / 创建 |
| GET/POST | /api/admin/projects | 项目列表 / 创建 `{tenant_id, name}` |
| PUT | /api/admin/projects/{pid}/members | 更新成员 `{user_id, role}` |
| DELETE | /api/admin/projects/{pid}/members/{uid} | 移除成员 |
| GET/POST | /api/admin/users | 用户列表 / 创建 |
| PUT | /api/admin/users/{uid} | 修改用户（角色/密码重置） |
| GET/POST/DELETE | /api/admin/projects/{pid}/api-keys[/{kid}] | 项目 API Key（OpenAI 兼容代理鉴权） |
| GET/POST/DELETE | /api/admin/guard-rules[/{rid}] | 护栏规则 |
| GET | /api/admin/audit | 审计日志（分页/过滤） |

## 数据集 /api/datasets

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/datasets?project_id= | 列表 |
| POST | /api/datasets | 创建 `{project_id, name, description}`（初始化 DVC 仓） |
| DELETE | /api/datasets/{id} | 删除（本地 + DVC + **S3 前缀**） |
| GET | /api/datasets/{id} | 详情 |
| GET | /api/datasets/{id}/versions | 版本列表 |
| POST | /api/datasets/{id}/versions/upload | 上传 JSONL/JSON/CSV 生成新版本（multipart `file`，`fmt`） |
| POST | /api/datasets/{id}/versions/{vid}/commit | DVC commit（`v{N}` 落 DVC rev，并同步 S3） |
| GET | /api/datasets/{id}/versions/{vid}/samples?page&size | 样本预览（自动回源 S3） |
| GET | /api/datasets/{id}/versions/{vid}/raw | 原始文档（解析产物） |
| GET | /api/datasets/{id}/dvc-log | DVC 提交历史 |
| POST | /api/datasets/parse | 文档解析（MinerU，multipart）→ 新版本 |
| POST | /api/datasets/clean | 清洗任务（backend 线程）→ 新版本 |
| POST | /api/datasets/synth | 合成任务（QA/CoT，调推理部署）→ 新版本 |
| POST | /api/datasets/{id}/annotate | 推送 Argilla 标注 |
| GET | /api/datasets/tasks | 数据任务队列（parse/clean/synth 状态轮询） |

## 模型 /api/models

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/models?project_id= | 资产列表（base/lora/merged + status） |
| POST | /api/models/base | 注册基座 `{project_id, name, hf_id}` → 后台下载 |
| POST | /api/models | 登记已有本地资产 `{path, type}` |
| GET | /api/models/{id} | 详情 |
| GET | /api/models/{id}/lineage | 血缘链 |
| DELETE | /api/models/{id} | 删除 |

## 训练 /api/training

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/training/jobs | 创建任务 `{project_id, name, stage(cpt/sft/dpo/export), finetuning_type(lora/full/freeze), base_model_id, dataset_version_ids[], hyperparams{}, deepspeed(none/zero2/zero3), gpu_count, priority, adapter_asset_id?}` |
| GET | /api/training/jobs?project_id&status | 列表 |
| GET | /api/training/jobs/{id} | 详情（含 checkpoints、container_name） |
| GET | /api/training/jobs/{id}/metrics | tensorboard 指标（loss/lr/epoch 序列） |
| GET | /api/training/jobs/{id}/logs?tail= | 运行日志尾部 |
| POST | /api/training/jobs/{id}/cancel | 取消（杀进程 + 删容器，保留 checkpoint） |
| POST | /api/training/jobs/{id}/resume | 断点续训（新建任务，`checkpoint` 空=最新） |
| GET | /api/training/params | **全量参数面（10 组 94 参数，前端表单驱动）** |
| GET | /api/training/templates | 阶段超参模板 |

## 评测 /api/evals

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/evals | 创建 `{project_id, dataset_version_id, target_deployment_id / target_asset_id, method(rule/judge/both/benchmark), judge_deployment_id?, benchmark?, limit?}` |
| GET | /api/evals | 列表 |
| GET | /api/evals/{id} | 详情（summary + details 分页） |
| POST | /api/evals/compare | 多任务对比 |
| DELETE | /api/evals/{id} | 删除 |

## 推理 /api/inference/deployments

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/inference/deployments | 创建 `{project_id, name, model_asset_id, engine(vllm/sglang), tensor_parallel, replicas, port?, env?}` |
| GET | /api/inference/deployments | 列表 |
| GET | /api/inference/deployments/{id} | 详情（endpoint / 容器 / 状态） |
| POST | /api/inference/deployments/{id}/start | 启动 |
| POST | /api/inference/deployments/{id}/stop | 停止 |
| POST | /api/inference/deployments/{id}/scale | 扩缩副本 `{replicas}` |
| GET | /api/inference/deployments/{id}/logs | 日志 |
| POST | /api/inference/deployments/{id}/adapters | 挂载 LoRA `{name, asset_id}`（vLLM 热加载） |
| DELETE | /api/inference/deployments/{id}/adapters/{name} | 卸载 LoRA |
| DELETE | /api/inference/deployments/{id} | 删除部署 |

部署完成后提供 **OpenAI 兼容端点**（`http://<ip>:8100+/v1/chat/completions`），
以项目 API Key 鉴权；护栏在输出侧生效。

## 集群 /api/cluster

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/cluster/nodes | GPU 节点（心跳/在线状态） |
| GET | /api/cluster/nodes/{id}/metrics | 历史利用率曲线 |
| GET | /api/cluster/queue | 排队 + 运行中任务 |
| PUT | /api/cluster/queue/{jid} | 调整优先级 `{priority}` |
| POST | /api/cluster/queue/{jid}/promote | 提至队首 |
| GET | /api/cluster/utilization | 当前 GPU 占用明细 |

## Dashboard /api/dashboard

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/dashboard/summary | 汇总：数据集/模型/任务/评测/部署计数、GPU 状态、**对象存储健康（storage）**、最近任务 |

## 通用约定

- 错误：HTTP 4xx/5xx + `{"detail": "..."}`；401/403 均写审计日志；
- 列表分页：多数接口 `?page&size` 或 `limit`（见具体实现）；
- 任务类实体状态机：`queued → running → succeeded / failed / cancelled`；
- 所有写操作需要对应项目角色：`owner/admin/developer`（viewer 只读）。

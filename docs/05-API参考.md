# API 参考

VerveDeck 平台后端 REST API 全量路由表。与代码 `platform/backend/app/api/*.py` 一一对应（13 个模块，约 110 个端点）。

## 通用约定

- **Base URL**：`https://<host>:443/api/...`（443 强制 HTTPS，经 web 反代；容器内网为 `backend:3001`；自签证书 curl 需加 `-k`）
- **认证**：`Authorization: Bearer <token>`。`POST /api/auth/login` 获取，默认 72h 有效（可由登录配置调整）；暴力破解保护：失败超限锁定用户
- **请求/响应**：JSON（文件上传除外，为 multipart/form-data）
- **错误格式**：`{"detail": "错误信息"}`，HTTP 状态码 400/401/403/404/409/422/502/503
- **RBAC**：角色 super_admin > tenant_admin > developer > viewer；标注权限的端点低于该角色返回 403
- **批量删除统一响应**（所有 `/batch-delete` 端点）：

  ```json
  {"deleted": [1, 2], "failed": [{"id": 3, "error": "被训练任务引用"}]}
  ```

  循环执行单删逻辑，单个失败不阻塞其余；被引用/不存在的条目进 `failed` 并附原因。
- **审计**：所有写操作与 401/403 拒绝均记入审计日志（`/api/admin/audit` 可查）

## 认证 /api/auth

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/auth/login | `{username, password}` → `{token, user}`；失败计次，超限锁定 |
| GET | /api/auth/me | 当前用户信息 |
| PUT | /api/auth/password | 修改密码 `{old_password, new_password}`（首次登录强制改密） |

## 租户与项目 /api/admin（super_admin；项目 owner 可管理本项目）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | /api/admin/tenants | 租户列表 / 创建 `{name}` |
| GET/POST | /api/admin/projects | 项目列表（含 members 用户 id 列表）/ 创建 `{tenant_id, name}` |
| PUT | /api/admin/projects/{pid}/members | 更新成员 `{user_id, role}` |
| DELETE | /api/admin/projects/{pid}/members/{uid} | 移除成员 |
| GET/POST | /api/admin/users | 用户列表 / 创建 `{username, password, role}` |
| PUT | /api/admin/users/{uid} | 修改用户（角色/重置密码） |

### API Key（OpenAI 兼容代理鉴权，可限定模型）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | /api/admin/projects/{pid}/api-keys | 列表 / 创建 `{name, models?}`（models 留空=不限） |
| DELETE | /api/admin/projects/{pid}/api-keys/{kid} | 删除（置 is_active=false） |
| POST | /api/admin/projects/{pid}/api-keys/batch-delete | 批量删除 `{ids}` |

### 护栏规则（内容安全拦截）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | /api/admin/guard-rules | 列表 / 创建（正则/关键词、输入/输出、作用域） |
| DELETE | /api/admin/guard-rules/{rid} | 删除 |
| POST | /api/admin/guard-rules/batch-delete | 批量删除 `{ids}` |

### 告警规则与通知

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | /api/admin/alerts/rules | 告警规则列表（分页 + 筛选 `{category,kind,enabled,q,limit,offset}` → `{total,items}`，容量 5000）/ 创建（支持 category 分类） |
| PUT | /api/admin/alerts/rules/{rid} | 更新规则（含分类） |
| DELETE | /api/admin/alerts/rules/{rid} | 删除 |
| POST | /api/admin/alerts/rules/batch | 批量创建（导入，单次 ≤1000；逐条校验、部分成功 `{created,failed}`） |
| GET | /api/admin/alerts/rules/categories | 现有分类去重列表（筛选/联想） |
| POST | /api/admin/alerts/rules/batch-delete | 批量删除 `{ids}` |
| POST | /api/admin/alerts/rules/{rid}/test | 试算规则（当前是否触发） |
| GET/PUT | /api/admin/notify-config | 通知通道配置（企业微信/邮件，DB 化） |
| POST | /api/admin/notify-config/test | 发送测试通知 |
| GET | /api/admin/alerts/events | 告警事件流 `{limit,level,target,acked}`（acked=1 已确认/0 未确认；含 acked_by/acked_at） |
| POST | /api/admin/alerts/events/ack | 批量确认/取消确认 `{ids,ack}`（≤1000） |
| POST | /api/admin/alerts/events/batch-delete | 批量删除事件 `{ids}`（≤1000） |

### 登录安全与系统配置

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/PUT | /api/admin/login-config | 登录安全参数（IP 白名单、失败锁定阈值/时长） |
| GET | /api/admin/audit | 审计日志（分页 + 动作/用户/状态过滤） |
| GET | /api/admin/audit/actions | 审计动作枚举 |
| GET/PUT | /api/admin/time-config | 时区配置 |
| POST | /api/admin/time-config/probe | 探测宿主时区 |

## 集群升级 /api/admin/upgrade（super_admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/admin/upgrade/status | 平台全量镜像清单（18 组件）升级状态（当前镜像 / 本地镜像 / 运行时版本 / 进行中任务） |
| POST | /api/admin/upgrade/check | `{target, force?}` → Docker Hub 可用版本列表（10min 缓存） |
| POST | /api/admin/upgrade/execute | `{target, to_version}` 创建升级任务（pull 换 tag / build 重建 + 蓝绿切容器） |
| GET | /api/admin/upgrade/tasks | 升级任务历史 `{limit}` |

组件（18 项，`upgrade_service.COMPONENTS` 为唯一数据源，与镜像管理页同序一致）：
- 引擎（pull 换 tag + 运行时 pin，仅新任务生效）：vllm / sglang / llamafactory / evalscope
- 容器（pull 换 tag + 蓝绿重建容器）：gpu_agent / dcgm_exporter / node_exporter / prometheus / alertmanager / grafana（深色+浅色双容器）/ postgres / redis / opensearch / argilla / rustfs / mineru
- 自构（build 以宿主 /repo 重建 + 蓝绿重建容器，自升级由调度器 converge 兜底）：backend / web

## 数据集 /api/datasets（developer+，租户内数据）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | /api/datasets | 列表（搜索/分页）/ 创建 |
| DELETE | /api/datasets/{dsid} | 删除（被引用时 409） |
| POST | /api/datasets/batch-delete | 批量删除 `{ids}` |
| GET | /api/datasets/tasks | 数据集后台任务状态 |
| GET | /api/datasets/template | 模板下载 |
| POST | /api/datasets/import-s3 | 从对象存储导入 |
| GET | /api/datasets/{dsid} | 详情 |
| GET | /api/datasets/{dsid}/versions | 版本列表（DVC） |
| POST | /api/datasets/{dsid}/versions/{vid}/commit | 提交新版本 |
| GET | /api/datasets/{dsid}/versions/{vid}/samples | 抽样查看 |
| GET | /api/datasets/{dsid}/versions/{vid}/raw | 原始文件 |
| GET | /api/datasets/{dsid}/dvc-log | DVC 操作日志 |
| POST | /api/datasets/{dsid}/versions/upload | 上传新版本（multipart） |
| POST | /api/datasets/parse | 文件解析（PDF/Office → 文本，MinerU） |
| POST | /api/datasets/clean | 数据清洗任务 |
| POST | /api/datasets/synth | 数据合成任务 |
| POST | /api/datasets/{dsid}/annotate | 标注任务（Argilla） |

## 模型仓库 /api/models（developer+）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/models | 资产列表（搜索/分类/血缘过滤） |
| POST | /api/models/base | 登记基座模型（本地路径/下载） |
| GET | /api/models/search | HF / ModelScope 搜索 |
| POST | /api/models/upload | 本地上传（multipart，断点续传） |
| POST | /api/models | 创建资产记录 |
| PUT | /api/models/{aid}/meta | 修改元数据（分类/说明） |
| GET | /api/models/{aid} | 详情 |
| GET | /api/models/{aid}/lineage | 血缘链（基座→LoRA→合并/量化产物） |
| DELETE | /api/models/{aid} | 删除（被引用时 409；量化产物有保护规则） |
| POST | /api/models/batch-delete | 批量删除 `{ids}` |

## 训练 /api/training（developer+）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/training/jobs | 创建训练任务（94 参数对齐 LLaMA-Factory；多机多卡：节点数×每节点卡数，torchrun/DDP/DeepSpeed/FSDP） |
| GET | /api/training/jobs | 任务列表 |
| GET | /api/training/jobs/{jid} | 详情（含完整训练参数快照） |
| GET | /api/training/jobs/{jid}/metrics | 训练曲线（loss/eval，经 Prometheus） |
| GET | /api/training/jobs/{jid}/logs | 实时日志 |
| POST | /api/training/jobs/{jid}/cancel | 取消（容器 pause 保留现场） |
| DELETE | /api/training/jobs/{jid} | 删除记录（产物受保护） |
| POST | /api/training/jobs/{jid}/resume | 恢复（从 pause 现场续跑） |
| GET | /api/training/params | 参数元数据（94 项 × 10 组） |
| GET | /api/training/templates | 训练模板 |

## 评测 /api/evals（developer+）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/evals | 创建评测任务（规则/LLM 评审，基线模型对比） |
| GET | /api/evals | 列表 |
| GET | /api/evals/{tid} | 详情 |
| GET | /api/evals/{tid}/report | 报告（JSON，可下载 Markdown） |
| POST | /api/evals/compare | 多任务对比 |
| DELETE | /api/evals/{tid} | 删除 |
| POST | /api/evals/batch-delete | 批量删除 `{ids}` |

## 模型压缩（量化）/api/quantize（developer+）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/quantize/methods | 量化方法元数据（GPTQ/AWQ/LLM.int8/GPTQ-v2 等，含硬件要求） |
| GET/POST | /api/quantize | 任务列表 / 创建（4 种量化方式；产物自动注册为 merged 资产并挂血缘） |
| GET | /api/quantize/{tid} | 详情 |
| GET | /api/quantize/{tid}/logs | 日志 |
| DELETE | /api/quantize/{tid} | 删除（已成功任务的已注册产物受保护，400 拒绝） |
| POST | /api/quantize/batch-delete | 批量删除 `{ids}` |

## 推理部署 /api/inference（developer+）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/inference/deployments | 创建部署（vLLM/SGLang，高级参数：max_num_seqs/dtype/prefix_caching/chunked_prefill/served_model_name/api_key/extra_args 等） |
| GET | /api/inference/deployments | 列表 |
| GET | /api/inference/deployments/{did} | 详情（含 openai_base_url 示例） |
| POST | /api/inference/deployments/{did}/start | 启动 |
| POST | /api/inference/deployments/{did}/stop | 停止 |
| DELETE | /api/inference/deployments/{did} | 删除（运行中先停） |
| POST | /api/inference/deployments/batch-delete | 批量删除 `{ids}`（运行中先 stop 再删） |
| POST | /api/inference/deployments/{did}/scale | 副本扩缩 `{replicas}` |
| GET | /api/inference/deployments/{did}/logs | 引擎日志 |
| PUT | /api/inference/deployments/{did} | 编辑部署（name/description/restart_on_error/backend 即时生效；引擎/副本/参数/扩展 KV/缓存关联自动重建；外键字段传 null 清除关联） |
| POST | /api/inference/deployments/{did}/scale-role | PD 角色扩缩 `{role, replicas}`（需空闲 GPU） |
| GET | /api/inference/deployments/{did}/instances | 实例视角（单实例 role/index/name/alive/ip + 自动重启退避信息） |
| POST | /api/inference/deployments/{did}/instances/rebuild | 单实例重建 `{role, index}`（部署级状态不变） |
| GET | /api/inference/deployments/{did}/metrics | 引擎指标（QPS/时延/KV 缓存，经 Prometheus） |
| POST | /api/inference/deployments/{did}/adapters | LoRA 热加载 `{name}`（不停机挂适配器） |
| DELETE | /api/inference/deployments/{did}/adapters/{name} | LoRA 热卸载 |

### 模型路由 /api/inference/routes（developer+，数据面走 Higress 网关）

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | /api/inference/routes | 创建 `{project_id, name, description, targets:[{deployment_id, weight, is_fallback}]}`（名与部署名互斥；权重 1-100 相对值） |
| GET | /api/inference/routes | 列表（`?project_id=` 过滤；含 targets 与部署状态） |
| GET | /api/inference/routes/{rid} | 详情（含 sync_status/sync_message） |
| PUT | /api/inference/routes/{rid} | 编辑（description/enabled/targets 全量替换；路由名不可改） |
| DELETE | /api/inference/routes/{rid} | 删除（网关残留由调度器孤儿清理摘除） |
| POST | /api/inference/routes/{rid}/targets | 加目标 `{deployment_id, weight, is_fallback}` |
| PUT | /api/inference/routes/{rid}/targets/{tid} | 改目标 `{weight, is_fallback}` |
| DELETE | /api/inference/routes/{rid}/targets/{tid} | 删目标 |

### 推理后端 /api/inference/backends（developer+）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/inference/backends | 列表（builtin/custom + 嵌套 versions + current_image） |
| POST | /api/inference/backends | 创建自定义后端（名须以 `-custom` 结尾） |
| PUT | /api/inference/backends/{bid} | 编辑（description/health_check_path/default_env/default_params；名不可改） |
| DELETE | /api/inference/backends/{bid} | 删除（builtin 403；被部署引用 409） |
| POST | /api/inference/backends/{bid}/versions | 加版本 `{version, image, run_command…}`（custom 必填 run_command；is_default 互斥） |
| PUT | /api/inference/backends/{bid}/versions/{vid} | 改版本（被引用的版本号 409 拒改） |
| DELETE | /api/inference/backends/{bid}/versions/{vid} | 删版本（最后一个 400；被引用 409） |
| POST | /api/inference/backends/import | YAML 导入（仅创建不覆盖，整批校验） |

### 缓存加速 /api/cache/services（developer+）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/cache/services | 列表（含 used_by 关联部署） |
| POST | /api/cache/services | 创建 `{name, image, command, env, port}` |
| PUT | /api/cache/services/{cid} | 编辑（运行中自动重启；名不可改） |
| DELETE | /api/cache/services/{cid} | 删除（被部署引用 409） |
| POST | /api/cache/services/{cid}/start | 启动（等镜像 → 容器 → 健康收敛） |
| POST | /api/cache/services/{cid}/stop | 停止 |

### OpenAI 兼容代理

`POST/GET /v1/{deploy_name}/{rest:path}`（经 web 443 反代）

- 鉴权：`Authorization: Bearer <项目 API Key>`（Key 限定了 models 时校验请求 model）
- 透传：`/v1/{deploy_name}/chat/completions`（含流式 SSE）、`/v1/{deploy_name}/models` 等
- 护栏：输入/输出内容匹配护栏规则时返回 451
- 部署非 running 时返回 503；served_model_name 映射自动归一请求体 model

## 集群运维 /api/cluster（developer+ 读；节点写需 super_admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/cluster/nodes | 计算节点列表（心跳/驱动/CUDA/宿主 CPU/内存/磁盘/网络/GPU 明细/DCGM 健康度） |
| POST | /api/cluster/nodes | 注册节点（多机扩展） |
| PUT | /api/cluster/nodes/{nid} | 更新节点 |
| DELETE | /api/cluster/nodes/{nid} | 移除节点 |
| GET | /api/cluster/nodes/{nid}/metrics | 节点指标历史 `{minutes}`（Prometheus） |
| GET | /api/cluster/system | 系统指标（CPU/内存/磁盘/网络/OS） |
| GET | /api/cluster/storage | 存储 IO（读/写吞吐 B/s + IO 延时 ms + IO 利用率 %，node-exporter；`{minutes}`） |
| GET | /api/cluster/queue | 任务队列（queued/running，GPU 占用） |
| PUT | /api/cluster/queue/{jid} | 调整优先级 `{priority 1-9}` |
| POST | /api/cluster/queue/{jid}/promote | 插队到队首 |
| GET | /api/cluster/utilization | 利用率汇总 |
| GET | /api/cluster/inference | 推理资源占用 |

## 监控与日志

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/dashboard/summary | Dashboard 汇总（任务/资源/告警聚合） |
| GET | /api/logs/services | 服务列表（容器） |
| GET | /api/logs | 容器日志 `{service, tail, ...}` |

## 对象存储 /api/storage（super_admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/storage | 存储概览（各后端用量） |
| GET | /api/storage/objects | 对象浏览 `{prefix, backend}`（S3 目录式） |
| POST | /api/storage/objects/batch-delete | 批量删除 `{names, backend}`（S3 幂等语义） |
| DELETE | /api/storage/objects | 单对象删除 `{name, backend}` |
| GET/POST | /api/storage/backends | 后端列表 / 添加（S3 兼容：endpoint/ak/sk/bucket） |
| POST | /api/storage/backends/{bid}/test | 连通性测试 |
| DELETE | /api/storage/backends/{bid} | 删除后端 |

## 镜像管理 /api/images（super_admin）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | /api/images | 平台全量镜像清单（18 项 = 5 引擎 + 3 自构 + 10 监控/基础设施，与集群升级页同一数据源同序；版本探测 5min 缓存 + stale 兜底非阻塞；自定义镜像追加末尾；pull/build 状态） |
| GET | /api/images/custom | 自定义镜像列表 |
| POST | /api/images/custom | 登记自定义镜像 `{name: tag, note}`（国产芯片等） |
| DELETE | /api/images/custom/{cid} | 移除登记 |
| POST | /api/images/custom/batch-delete | 批量移除 `{ids}` |
| POST | /api/images/{key}/probe | 强制版本探测（同步；key 为 18 组件键；无探测命令的组件返回当前运行时版本） |
| POST | /api/images/{key}/refresh | 后台拉取/重建 `{force?}`（异步，状态经 GET /api/images 轮询） |

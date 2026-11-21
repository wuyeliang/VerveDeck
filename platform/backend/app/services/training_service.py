"""训练任务服务（LLaMA-Factory 引擎）

架构：每个训练任务 = 每个节点一个独立 pause 占位容器，exec 拉起训练进程。
- 单机单卡：单容器 + 原生 llamafactory-cli（与旧版一致）
- 单机多卡 / 多机多卡：每节点一个容器（共享 /data），统一走原生
  llamafactory-cli，由镜像内 launcher 读取 rendezvous 环境变量自启 torchrun
  （rank0 为主节点 MASTER，跨节点经容器网络互达，NCCL 支持 IB/RDMA）
- export（合并 LoRA）：恒单进程 `llamafactory-cli export`（非分布式任务）
- 训练框架：原生 DDP / DeepSpeed ZeRO-2/3 / PyTorch FSDP（job.deepspeed 字段）
- GPU 设备在容器创建时按每节点卡数独占绑定（资源隔离）
- 配置/日志/tensorboard 全部落在 /data/jobs/<id>/
- 断点续训：基于输出目录中的 checkpoint-xxx 新建任务
"""
import json
import logging
import os
import glob
import time
import uuid

import yaml

from ..config import settings
from ..models import TrainingJob, ModelAsset, Dataset, DatasetVersion
from ..database import SessionLocal
from . import taskcontainer as tc
from . import dataset_service as ds
from . import upgrade_service

log = logging.getLogger(__name__)

STAGE_MAP = {"cpt": "pretrain", "pretrain": "pretrain", "sft": "sft", "dpo": "dpo", "export": "export"}

# 训练镜像 tensorboard 可用性探测缓存（image -> bool，每镜像只探测一次）
_tb_probe_cache: dict[str, bool] = {}


def image_has_tensorboard(image: str) -> bool:
    """探测训练镜像内是否可 import tensorboard（按镜像名缓存，探测开销约数秒）。

    官方预构建镜像（hiyouga/llamafactory）未安装 tensorboard，此时 report_to=tensorboard
    会在 transformers TensorBoardCallback 初始化处直接 RuntimeError。探测失败
    （镜像未就绪/docker 异常等）按「不可用」保守处理。
    """
    if image in _tb_probe_cache:
        return _tb_probe_cache[image]
    ok = False
    name = f"ft-tb-probe-{uuid.uuid4().hex[:8]}"
    try:
        client = tc.docker_client()
        cid = client.api.create_container(
            image, command=["python3", "-c", "import tensorboard"],
            entrypoint=["python3"], name=name, stdout=False, stderr=False)
        client.api.start(cid)
        res = client.api.wait(cid, timeout=120)
        ok = res.get("StatusCode", -1) == 0
    except Exception as e:
        log.warning("tensorboard 探测失败 image=%s: %s", image, e)
    finally:
        tc.remove_container(name, force=True)
    _tb_probe_cache[image] = ok
    if not ok:
        log.warning("训练镜像 %s 无 tensorboard，report_to=tensorboard/all 将降级为 none", image)
    return ok


def job_dir(jid: int) -> str:
    """训练任务工作目录：/data/jobs/<id>/（config.yaml / run.log / ckpt / runs / merged）"""
    return os.path.join(settings.data_dir, "jobs", str(jid))


# ---------------------------------------------------------------------------
# 分布式训练编排（单机多卡 / 多机多卡，LLaMA-Factory 原生 launcher + torchrun）
#   - 每节点一个任务容器（共享 /data），rank0 为主节点（MASTER）
#   - 统一命令 llamafactory-cli <sub> cfg；多卡/多机由镜像内 launcher 读
#     rank_launch_env 注入的 rendezvous 环境变量自启 torchrun（见 training_cmd）
#   - 单卡/export：单进程，命令/容器/env 与旧版字节级一致（保证回归不变）
# ---------------------------------------------------------------------------
MASTER_PORT_BASE = 29500


def job_gpu_need(job) -> int:
    """任务总卡数 = 节点数 × 每节点卡数。

    兼容旧任务：旧行 node_count/nproc_per_node 默认为 1，但 gpu_count 可能 > 1
    （旧「单节点多卡」写法），取两者较大值，避免准入/统计低估。
    """
    nn = max(1, int(job.node_count or 1))
    nproc = max(1, int(job.nproc_per_node or 1))
    return max(int(job.gpu_count or 1), nn * nproc)


def rank_container_names(job) -> list:
    """各 rank 任务容器名。rank0 沿用旧命名 ft-train-<id>（单卡回退不变）。"""
    nn = max(1, int(job.node_count or 1))
    return [f"ft-train-{job.id}"] + [f"ft-train-{job.id}-r{k}" for k in range(1, nn)]


def rank_task_keys(job) -> list:
    """各 rank 的 exec 任务键。rank0=train-<id>（与旧版一致，供调度器收敛轮询）。"""
    nn = max(1, int(job.node_count or 1))
    return [f"train-{job.id}"] + [f"train-{job.id}-r{k}" for k in range(1, nn)]


def rank_log_paths(job) -> list:
    """各 rank 的 run.log 路径（rank0 为主日志，其余 run_r<k>.log）。"""
    base = job_dir(job.id)
    nn = max(1, int(job.node_count or 1))
    return [os.path.join(base, "run.log")] + \
           [os.path.join(base, f"run_r{k}.log") for k in range(1, nn)]


def master_port(job) -> int:
    """torchrun rendezvous 端口：按 job.id 错开，避免同机并发多任务端口冲突。"""
    return MASTER_PORT_BASE + (int(job.id or 0) % 1000)


def container_env(job) -> dict:
    """训练容器环境变量（含 NCCL 通信配置，IB/RDMA 友好）。

    - 多卡默认注入 NCCL_P2P_DISABLE=1（4090/PCIe P2P 不稳兜底）
    - 多机默认 NCCL_DEBUG=INFO（便于排障）
    - IB/RDMA：用户经 job.nccl_env 覆盖（NCCL_IB_HCA / NCCL_SOCKET_IFNAME /
      NCCL_NET_GDR_LEVEL / NCCL_IB_GID_INDEX / NCCL_P2P_DISABLE=0 等），优先级最高
    """
    env = {"OMP_NUM_THREADS": "4"}
    total = job_gpu_need(job)
    nn = max(1, int(job.node_count or 1))
    if total > 1:
        env["NCCL_P2P_DISABLE"] = "1"
    if nn > 1:
        env.setdefault("NCCL_DEBUG", "INFO")
        env.setdefault("NCCL_SHM_DISABLE", "0")
    user = job.nccl_env or {}
    if isinstance(user, dict):
        env.update({str(k): str(v) for k, v in user.items()})
    return env


def training_cmd(job, cfg: str, rank: int, nnodes: int, nproc: int,
                 master_addr: str, port: int) -> list:
    """构造单 rank 容器内的启动命令。

    统一走原生 `llamafactory-cli <sub> {cfg}`：
    - export 恒单进程（合并模型不是分布式任务，多进程并发写同一 export_dir 不合法）；
    - 多卡/多机训练不再由平台外层自套 torchrun：镜像内 launcher（llamafactory-cli
      train）读取 NNODES/NODE_RANK/NPROC_PER_NODE/MASTER_ADDR/MASTER_PORT 环境变量
      （见 rank_launch_env）自行编排 torchrun（LF 原生多机契约，worker 经
      `python launcher.py` 的 __main__ 守卫直达 run_exp，不会二次重入）。
      平台若另套 `torchrun -m llamafactory.cli train`，worker 会重入 launcher 的
      train 分支再启一层 torchrun，与外层争抢同一 MASTER_PORT → EADDRINUSE
      （2026-11-18 双卡 ZeRO-2 e2e 实测踩中）。
    - 单卡/单进程场景与旧版命令字节级一致。
    """
    sub = "export" if job.stage == "export" else "train"
    return ["bash", "-lc", f"cd /data && llamafactory-cli {sub} {cfg} 2>&1"]


def rank_launch_env(job, rank: int, master_addr: str, port: int) -> dict:
    """每 rank 的 exec 级环境变量：镜像内 launcher 消费的分布式 rendezvous 参数。

    - export / 单机单卡：空（走单进程路径，与旧版 env 一致）；
    - 多卡/多机：注入 5 个 rendezvous 变量，launcher 据此自启 torchrun
      （--nnodes/--node_rank/--nproc_per_node/--master_addr/--master_port）；
    - 多机×每节点单卡：get_device_count()==1 不会进入 launcher 的自动分布式
      分支，需 FORCE_TORCHRUN=1 强制（launcher 自身对 USE_MCA 也是此写法）。
    """
    if job.stage == "export":
        return {}
    nnodes = max(1, int(job.node_count or 1))
    nproc = max(1, int(job.nproc_per_node or 1))
    if nnodes == 1 and nproc == 1:
        return {}
    env = {"NNODES": str(nnodes), "NODE_RANK": str(rank),
           "NPROC_PER_NODE": str(nproc), "MASTER_ADDR": master_addr,
           "MASTER_PORT": str(port)}
    if nproc == 1:
        env["FORCE_TORCHRUN"] = "1"
    return env


def resolve_master_addr(cids: list, nnodes: int) -> str:
    """多机取 rank0 容器 IP 作 MASTER_ADDR（fintune_net 内互达）；单机/单卡走回环。

    容器 IP 由 docker 在 create/run 时同步分配，正常一次可得；重试兜底网络未就绪。
    """
    if nnodes <= 1:
        return "127.0.0.1"
    for _ in range(10):
        ip = tc.container_ip(cids[0])
        if ip:
            return ip
        time.sleep(1)
    raise RuntimeError("无法获取主容器 IP（fintune_net 未就绪），多机训练需容器网络互通")


# DeepSpeed ZeRO 配置模板（内嵌，替代原镜像内 /workspace/ds_*.json 拷贝，
# 兼容官方预构建镜像；batch/accum 用 "auto" 由 LF 依据 per_device 配置回填）
DEEPSPEED_CONFIGS: dict[str, dict] = {
    "zero2": {
        "train_batch_size": "auto",
        "train_micro_batch_size_per_gpu": "auto",
        "gradient_accumulation_steps": "auto",
        "gradient_clipping": "auto",
        "steps_per_print": 2000,
        "wall_clock_breakdown": False,
        "zero_optimization": {
            "stage": 2,
            "allgather_partitions": True,
            "allgather_bucket_size": 2e8,
            "overlap_comm": True,
            "reduce_scatter": True,
            "reduce_bucket_size": 2e8,
            "contiguous_gradients": True,
        },
    },
    "zero3": {
        "train_batch_size": "auto",
        "train_micro_batch_size_per_gpu": "auto",
        "gradient_accumulation_steps": "auto",
        "gradient_clipping": "auto",
        "steps_per_print": 2000,
        "zero_optimization": {
            "stage": 3,
            "overlap_comm": True,
            "contiguous_gradients": True,
            "reduce_bucket_size": 2e8,
            "stage3_prefetch_bucket_size": 2e8,
            "stage3_param_persistence_threshold": 1e6,
            "stage3_max_live_parameters": 1e9,
            "stage3_max_reuse_distance": 1e9,
            "stage3_gather_16bit_weights_on_model_save": True,
        },
    },
}

# ---------------------------------------------------------------------------
# LLaMA-Factory 全量训练参数面（对齐 WebUI 页面，key 与 LF 0.9.3 参数一一对应；
# 经镜像内 llamafactory 0.9.3 参数字段核实 + HF TrainingArguments 继承字段）
# type: int/float/bool/str；default 为平台默认值；None=无默认（用户未填则不写入配置）
# ---------------------------------------------------------------------------
PARAM_GROUPS: list[dict] = [
    {"key": "data", "label": "数据与序列", "open": True, "params": [
        {"key": "template", "label": "对话模板 template", "type": "str", "default": "qwen",
         "choices": ["qwen", "llama3", "chatml", "glm4", "deepseek", "default"]},
        {"key": "cutoff_len", "label": "截断长度 cutoff_len", "type": "int", "default": 2048, "min": 8, "max": 131072},
        {"key": "packing", "label": "样本打包 packing", "type": "bool", "default": False},
        {"key": "neat_packing", "label": "按样本边界打包 neat_packing", "type": "bool", "default": False},
        {"key": "streaming", "label": "流式读取 streaming", "type": "bool", "default": False},
        {"key": "max_samples", "label": "每数据集最大样本数 max_samples", "type": "int", "default": 100000, "min": 1},
        {"key": "val_size", "label": "验证集比例 val_size", "type": "float", "default": 0, "min": 0, "max": 1, "step": 0.001},
        {"key": "train_on_prompt", "label": "对 prompt 计算损失 train_on_prompt", "type": "bool", "default": False},
        {"key": "mask_history", "label": "多轮仅最后轮计算损失 mask_history", "type": "bool", "default": False},
        {"key": "neftune_noise_alpha", "label": "NEFTune 噪声 neftune_noise_alpha", "type": "float", "default": 0, "min": 0, "max": 10, "step": 0.1},
        {"key": "preprocessing_num_workers", "label": "数据预处理进程数", "type": "int", "default": 4, "min": 1},
        {"key": "dataloader_num_workers", "label": "dataloader 进程数", "type": "int", "default": 1, "min": 0},
        {"key": "dataloader_pin_memory", "label": "dataloader_pin_memory", "type": "bool", "default": True},
        {"key": "dataloader_persistent_workers", "label": "dataloader_persistent_workers", "type": "bool", "default": False},
        {"key": "dataloader_prefetch_factor", "label": "dataloader_prefetch_factor", "type": "int", "default": 10, "min": 1},
    ]},
    {"key": "train", "label": "训练策略", "open": True, "params": [
        {"key": "learning_rate", "label": "学习率 learning_rate", "type": "float", "default": 1e-4, "min": 0},
        {"key": "num_train_epochs", "label": "轮数 num_train_epochs", "type": "float", "default": 3.0, "min": 0.1, "step": 0.5},
        {"key": "max_steps", "label": "最大步数 max_steps（-1=按轮数）", "type": "int", "default": -1, "min": -1},
        {"key": "lr_scheduler_type", "label": "学习率调度", "type": "str", "default": "cosine",
         "choices": ["cosine", "linear", "constant", "constant_with_warmup", "polynomial", "inverse_sqrt",
                     "reduce_lr_on_plateau", "cosine_with_min_lr"]},
        {"key": "warmup_ratio", "label": "warmup 比例", "type": "float", "default": 0.1, "min": 0, "max": 1, "step": 0.01},
        {"key": "warmup_steps", "label": "warmup 步数（>0 时覆盖比例）", "type": "int", "default": 0, "min": 0},
        {"key": "optim", "label": "优化器 optim", "type": "str", "default": "adamw_torch",
         "choices": ["adamw_torch", "adamw_hf", "adamw_8bit", "adamw_bnb_8bit", "sgd", "adafactor"]},
        {"key": "adam_beta1", "label": "adam_beta1", "type": "float", "default": 0.9, "min": 0, "max": 1},
        {"key": "adam_beta2", "label": "adam_beta2", "type": "float", "default": 0.999, "min": 0, "max": 1},
        {"key": "adam_epsilon", "label": "adam_epsilon", "type": "float", "default": 1e-8, "min": 0},
        {"key": "weight_decay", "label": "weight_decay", "type": "float", "default": 0, "min": 0, "max": 1},
        {"key": "max_grad_norm", "label": "梯度裁剪 max_grad_norm（0=关闭）", "type": "float", "default": 1.0, "min": 0},
        {"key": "per_device_train_batch_size", "label": "单卡 batch_size", "type": "int", "default": 1, "min": 1},
        {"key": "per_device_eval_batch_size", "label": "验证 batch_size", "type": "int", "default": 1, "min": 1},
        {"key": "gradient_accumulation_steps", "label": "梯度累积 grad_accum", "type": "int", "default": 8, "min": 1},
        {"key": "save_steps", "label": "保存间隔 save_steps", "type": "int", "default": 200, "min": 1},
        {"key": "save_total_limit", "label": "checkpoint 保留数", "type": "int", "default": 3, "min": 1},
        {"key": "save_only_model", "label": "仅保存权重 save_only_model", "type": "bool", "default": False},
        {"key": "save_strategy", "label": "保存策略", "type": "str", "default": "steps", "choices": ["no", "steps", "epoch", "best"]},
        {"key": "logging_steps", "label": "日志间隔 logging_steps", "type": "int", "default": 5, "min": 1},
        {"key": "eval_strategy", "label": "验证策略 eval_strategy", "type": "str", "default": "no", "choices": ["no", "steps", "epoch"]},
        {"key": "gradient_checkpointing", "label": "梯度检查点（省显存）", "type": "bool", "default": True},
        {"key": "seed", "label": "随机种子 seed", "type": "int", "default": 42},
        {"key": "enable_thinking", "label": "思考模式 enable_thinking（Qwen3 等）", "type": "bool", "default": True},
        {"key": "report_to", "label": "实验上报 report_to", "type": "str", "default": "tensorboard",
         "choices": ["none", "wandb", "mlflow", "neptune", "tensorboard", "swanlab", "all"]},
        {"key": "flash_attn", "label": "Flash Attention", "type": "str", "default": "auto", "choices": ["auto", "fa2", "sdpa", "eager"]},
        {"key": "rope_scaling", "label": "RoPE 扩展 rope_scaling", "type": "str", "default": None, "choices": ["linear", "dynamic"]},
        {"key": "max_new_tokens", "label": "验证生成最大长度", "type": "int", "default": 512, "min": 1},
    ]},
    {"key": "precision", "label": "精度与量化", "open": False, "params": [
        {"key": "compute_type", "label": "训练精度", "type": "str", "default": "bf16",
         "choices": ["bf16", "fp16", "fp32", "pure_bf16"]},
        {"key": "quantization_bit", "label": "QLoRA 量化位（0=不量化）", "type": "int", "default": 0, "choices": [0, 4, 8]},
        {"key": "quantization_method", "label": "量化方法", "type": "str", "default": "bitsandbytes",
         "choices": ["bitsandbytes", "gptq", "auto-gptq", "hqq", "eetq"]},
    ]},
    {"key": "lora", "label": "LoRA", "open": True, "params": [
        {"key": "lora_rank", "label": "lora_rank", "type": "int", "default": 8, "min": 1, "max": 1024},
        {"key": "lora_alpha", "label": "lora_alpha", "type": "int", "default": 32, "min": 1, "max": 2048},
        {"key": "lora_dropout", "label": "lora_dropout", "type": "float", "default": 0.05, "min": 0, "max": 1, "step": 0.01},
        {"key": "lora_target", "label": "lora_target（all 或模块名）", "type": "str", "default": "all"},
        {"key": "additional_target", "label": "额外 LoRA 模块 additional_target", "type": "str", "default": None},
        {"key": "use_rslora", "label": "RSLoRA use_rslora", "type": "bool", "default": False},
        {"key": "use_dora", "label": "DoRA use_dora", "type": "bool", "default": False},
        {"key": "pissa", "label": "PiSSA 初始化", "type": "bool", "default": False},
        {"key": "loraplus_lr_ratio", "label": "LoRA+ 学习率比", "type": "float", "default": 0, "min": 0, "max": 64, "step": 0.5},
        {"key": "create_new_adapter", "label": "create_new_adapter", "type": "bool", "default": False},
    ]},
    {"key": "freeze", "label": "Freeze 冻结", "open": False, "params": [
        {"key": "freeze_trainable_layers", "label": "可训练层数（-1=全部）", "type": "int", "default": 2, "min": -128, "max": 128},
        {"key": "freeze_trainable_modules", "label": "可训练模块（all 或名称）", "type": "str", "default": "all"},
        {"key": "freeze_extra_modules", "label": "额外冻结模块", "type": "str", "default": None},
    ]},
    {"key": "dpo", "label": "DPO 偏好对齐", "open": False, "params": [
        {"key": "pref_beta", "label": "KL 系数 pref_beta", "type": "float", "default": 0.1, "min": 0, "max": 1, "step": 0.01},
        {"key": "pref_ftx", "label": "SFT 损失权重 pref_ftx", "type": "float", "default": 0, "min": 0, "max": 10, "step": 0.01},
        {"key": "pref_loss", "label": "偏好损失类型", "type": "str", "default": "sigmoid",
         "choices": ["sigmoid", "hinge", "ipo", "kto_pair", "orpo", "simpo"]},
    ]},
    {"key": "mm", "label": "多模态（VLM）", "open": False, "params": [
        {"key": "freeze_vision_tower", "label": "冻结视觉塔", "type": "bool", "default": True},
        {"key": "freeze_multi_modal_projector", "label": "冻结多模态投影", "type": "bool", "default": True},
        {"key": "freeze_language_model", "label": "冻结语言模型", "type": "bool", "default": False},
        {"key": "image_max_pixels", "label": "图像最大像素", "type": "str", "default": "768*768"},
        {"key": "image_min_pixels", "label": "图像最小像素", "type": "str", "default": "32*32"},
        {"key": "video_max_pixels", "label": "视频最大像素", "type": "str", "default": "256*256"},
        {"key": "video_min_pixels", "label": "视频最小像素", "type": "str", "default": "16*16"},
    ]},
    {"key": "mem", "label": "显存优化（GaLoRE/Apollo/BAdam）", "open": False, "params": [
        {"key": "use_galore", "label": "启用 GaLoRE", "type": "bool", "default": False},
        {"key": "galore_rank", "label": "galore_rank", "type": "int", "default": 16, "min": 1, "max": 1024},
        {"key": "galore_update_interval", "label": "galore_update_interval", "type": "int", "default": 200, "min": 1},
        {"key": "galore_scale", "label": "galore_scale", "type": "float", "default": 2.0, "step": 0.1},
        {"key": "galore_target", "label": "galore_target", "type": "str", "default": "all"},
        {"key": "use_apollo", "label": "启用 Apollo", "type": "bool", "default": False},
        {"key": "apollo_rank", "label": "apollo_rank", "type": "int", "default": 16, "min": 1, "max": 1024},
        {"key": "apollo_update_interval", "label": "apollo_update_interval", "type": "int", "default": 200, "min": 1},
        {"key": "apollo_scale", "label": "apollo_scale", "type": "float", "default": 32.0, "step": 0.1},
        {"key": "apollo_target", "label": "apollo_target", "type": "str", "default": "all"},
        {"key": "use_badam", "label": "启用 BADAM", "type": "bool", "default": False},
        {"key": "badam_mode", "label": "badam_mode", "type": "str", "default": "layer", "choices": ["layer", "ratio"]},
        {"key": "badam_switch_mode", "label": "badam_switch_mode", "type": "str", "default": "ascending",
         "choices": ["ascending", "descending", "random", "fixed"]},
        {"key": "badam_switch_interval", "label": "badam_switch_interval", "type": "int", "default": 50, "min": 1},
        {"key": "badam_update_ratio", "label": "badam_update_ratio", "type": "float", "default": 0.05, "min": 0, "max": 1, "step": 0.01},
    ]},
    {"key": "export", "label": "导出（合并 LoRA）", "open": False, "params": [
        {"key": "export_size", "label": "导出分片大小(GB)", "type": "int", "default": 2, "min": 1},
        {"key": "export_device", "label": "导出设备", "type": "str", "default": "cpu", "choices": ["cpu", "cuda"]},
        {"key": "export_legacy_format", "label": "旧格式分片 export_legacy_format", "type": "bool", "default": False},
    ]},
    {"key": "other", "label": "其他", "open": False, "params": [
        {"key": "cache_dir", "label": "模型缓存目录 cache_dir", "type": "str", "default": None},
        {"key": "resume_from_checkpoint", "label": "断点续训 checkpoint 路径", "type": "str", "default": None},
        {"key": "extra_args", "label": "额外参数 extra_args（JSON，最高优先级）", "type": "str", "default": None},
    ]},
]

# 平台内部装配、不接受前端覆盖的键
INTERNAL_KEYS = {
    "model_name_or_path", "stage", "do_train", "finetuning_type", "output_dir",
    "logging_dir", "dataset", "dataset_dir", "adapter_name_or_path",
    "export_dir", "deepspeed", "overwrite_output_dir", "plot_loss",
    "trust_remote_code", "ddp_timeout",
}

# 镜像内 LLaMA-Factory 0.9.3 不支持的键（HfArgumentParser 会因「未使用」而报错）。
# 已从 PARAM_GROUPS 移除（UI 不再提供）；此处兜底剥离，防止旧任务 hyperparams /
# extra_args 透传残留导致训练容器启动即失败。
UNSUPPORTED_LF_KEYS = {
    "bias", "early_stopping_patience", "early_stopping_threshold", "lazy_tokenize",
}

# export（合并模型）专用剥离集：LF 的 export 解析器只接受
# Model/Data/Finetuning/Generating 四个 dataclass（无 TrainingArguments），
# 训练键残留会触发 "Some keys are not used by the HfArgumentParser" 直接失败
# （2026-11-18 export e2e 实测踩中）。plot_loss/neftune_noise_alpha 属
# Finetuning/Model dataclass，export 合法，不剥离。
EXPORT_STRIP_KEYS = {
    # 平台基础装配（训练输出/上报相关）
    "output_dir", "logging_dir", "overwrite_output_dir", "report_to",
    "ddp_timeout", "deepspeed",
    # 训练超参
    "learning_rate", "optim", "lr_scheduler_type", "warmup_ratio", "warmup_steps",
    "weight_decay", "max_grad_norm", "adam_beta1", "adam_beta2", "adam_epsilon",
    "per_device_train_batch_size", "per_device_eval_batch_size",
    "gradient_accumulation_steps", "gradient_checkpointing",
    "logging_steps", "save_steps", "save_strategy", "save_total_limit",
    "save_only_model", "eval_strategy", "max_steps", "num_train_epochs", "seed",
    # 精度（bf16/fp16 属 TrainingArguments；pure_bf16 属 ModelArguments，保留）
    "fp16", "bf16",
    # dataloader / FSDP
    "dataloader_num_workers", "dataloader_persistent_workers",
    "dataloader_pin_memory", "dataloader_prefetch_factor",
    "fsdp", "fsdp_use_orig_params", "fsdp_transformer_layer_cls_to_wrap",
}


def param_spec() -> list[dict]:
    """返回全量参数面（GET /api/training/params，前端动态表单驱动）"""
    return PARAM_GROUPS


def _active_groups(job, stage: str) -> set:
    """按 阶段/微调方式 决定哪些参数组生效（对齐 WebUI 表单的条件显隐）"""
    act = {"data", "train", "precision", "mm", "mem", "other"}
    if job.finetuning_type == "lora" and stage != "export":
        act.add("lora")
    if job.finetuning_type == "freeze":
        act.add("freeze")
    if stage in ("dpo", "kto"):
        act.add("dpo")
    if stage == "export":
        act.add("export")
    return act


def _coerce(v, t: str):
    """按 spec 类型转换前端/用户传入值；失败返回 None（该 key 不写入配置）"""
    if t == "int":
        try:
            return int(float(v))
        except (TypeError, ValueError):
            return None
    if t == "float":
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if t == "bool":
        if isinstance(v, str):
            return v.strip().lower() in ("1", "true", "yes", "on")
        return bool(v)
    return str(v)


def _apply_conditionals(d: dict, hp: dict, job, db, stage: str) -> None:
    """按 阶段/微调方式/精度 派生 LF 配置键（对齐 WebUI runner._parse_train_args）"""
    ft = job.finetuning_type
    # 精度：compute_type → fp16 / bf16 / pure_bf16（fp32 = 全关）
    ct = hp.get("compute_type") or d.get("compute_type") or "bf16"
    d["fp16"] = (ct == "fp16")
    d["bf16"] = (ct == "bf16")
    d["pure_bf16"] = (ct == "pure_bf16")
    d.pop("compute_type", None)
    # LoRA 派生
    if ft == "lora" and stage != "export":
        d.setdefault("lora_target", "all")
        if d.pop("pissa", False) or hp.get("pissa"):
            d["pissa_init"] = True
            d["pissa_convert"] = True
    else:
        d.pop("pissa", None)
    # DPO/KTO
    if stage in ("dpo", "kto"):
        d.setdefault("pref_beta", 0.1)
    # 空值/零值清理（LF 解析器对 None 语义 = 未设置）
    for k in ("rope_scaling", "additional_target", "cache_dir", "freeze_extra_modules"):
        if not d.get(k):
            d.pop(k, None)
    if not d.get("neftune_noise_alpha"):
        d.pop("neftune_noise_alpha", None)
    if not d.get("loraplus_lr_ratio"):
        d.pop("loraplus_lr_ratio", None)
    qb = d.get("quantization_bit")
    if not qb:
        d.pop("quantization_bit", None)
        d.pop("quantization_method", None)
    # extra_args：用户显式 JSON 透传（最高优先级，覆盖 spec 值）
    ea = hp.get("extra_args")
    if isinstance(ea, str) and ea.strip():
        try:
            merged = json.loads(ea)
            if isinstance(merged, dict):
                d.update(merged)
        except Exception:
            import logging
            logging.getLogger(__name__).warning("extra_args 非合法 JSON，已忽略: %s", ea[:200])
    elif isinstance(ea, dict):
        d.update(ea)
    d.pop("extra_args", None)
    # export（合并 LoRA → 完整模型）
    if stage == "export":
        adapter = db.get(ModelAsset, job.adapter_asset_id) if job.adapter_asset_id else None
        d["do_train"] = False
        d["adapter_name_or_path"] = adapter.path if adapter else ""
        d["export_dir"] = f"{job_dir(job.id)}/merged"
        d.pop("do_train", None)


def build_config(job: TrainingJob, db) -> dict:
    """生成 LLaMA-Factory 训练配置 dict（spec 驱动全量参数，见 PARAM_GROUPS）"""
    base = db.get(ModelAsset, job.base_model_id)
    hp = {k: v for k, v in (job.hyperparams or {}).items() if v not in (None, "")}
    stage = STAGE_MAP.get(job.stage, "sft")

    d = {
        "model_name_or_path": base.path if base else "",
        "stage": stage,
        "do_train": stage != "export",
        "finetuning_type": job.finetuning_type if stage != "export" else "lora",
        "output_dir": f"{job_dir(job.id)}/ckpt",
        "logging_dir": f"{job_dir(job.id)}/runs",
        "plot_loss": True,
        "overwrite_output_dir": True,
        "report_to": "tensorboard",
        "trust_remote_code": True,
        "ddp_timeout": 180000000,
    }

    active = _active_groups(job, stage)
    # 1) spec 默认值（仅激活组，用户未覆盖时生效）
    for g in PARAM_GROUPS:
        if g["key"] not in active:
            continue
        for p in g["params"]:
            k = p["key"]
            if k in hp or k in INTERNAL_KEYS or p.get("default") is None:
                continue
            d[k] = _coerce(p["default"], p["type"])
    # 2) 用户覆盖（仅激活组已知键，按类型转换；未激活组的键忽略，与 WebUI 一致）
    active_keys = set()
    for g in PARAM_GROUPS:
        if g["key"] not in active:
            continue
        for p in g["params"]:
            k = p["key"]
            active_keys.add(k)
            if k in hp and k not in INTERNAL_KEYS:
                v = _coerce(hp[k], p["type"])
                if v is not None:
                    d[k] = v
    # 3) 未知键透传（前向兼容 LF 后续新增参数）
    spec_keys = {p["key"] for g in PARAM_GROUPS for p in g["params"]}
    for k, v in hp.items():
        if k not in spec_keys and k not in INTERNAL_KEYS:
            d[k] = v
    # 4) 条件派生（精度/LoRA/DPO/量化/export/extra_args）
    _apply_conditionals(d, hp, job, db, stage)

    # 数据集：dataset_dir 指向任务目录（内含 dataset_info.json + 数据软链）
    dataset_names = []
    dataset_info = {}
    # 先建任务目录，否则下方 os.symlink 的目标父目录不存在 -> FileNotFoundError
    os.makedirs(job_dir(job.id), exist_ok=True)
    for vid in (job.dataset_version_ids or []):
        ver = db.get(DatasetVersion, vid)
        if not ver:
            continue
        dset = db.get(Dataset, ver.dataset_id)
        name = f"ds{ver.id}"
        repo = ds.dataset_repo_dir(dset.project_id, dset.name, dset=dset, db=db)
        src = ds.ensure_version_local(repo, ver.version, dset=dset, db=db)
        link = f"{job_dir(job.id)}/{name}.jsonl"
        if not os.path.exists(link):
            if os.path.exists(src):
                os.symlink(src, link)
        if ver.fmt == "dpo":
            dataset_info[name] = {"file_name": f"{name}.jsonl", "formatting": "preference",
                                  "columns": {"prompt": "prompt", "chosen": "chosen", "rejected": "rejected"}}
        else:
            dataset_info[name] = {"file_name": f"{name}.jsonl", "formatting": "alpaca",
                                  "columns": {"prompt": "instruction", "query": "input", "response": "output"}}
        dataset_names.append(name)
    if dataset_names:
        d["dataset"] = ",".join(dataset_names)
        d["dataset_dir"] = job_dir(job.id)
        with open(os.path.join(job_dir(job.id), "dataset_info.json"), "w", encoding="utf-8") as f:
            json.dump(dataset_info, f, ensure_ascii=False, indent=1)

    # 分布式训练框架（job.deepspeed 现扩展为全量框架选择器）：
    #   zero2/zero3 → 写 DeepSpeed 配置；fsdp → 注入 HF FSDP 参数；none(原生 DDP) 不写额外配置。
    # DeepSpeed：模板内嵌于后端（不依赖镜像内 /workspace/ds_*.json，兼容官方预构建镜像），
    # 按需写出到任务工作目录（容器内 /data/jobs/<id>/ 可见）
    if job.deepspeed in DEEPSPEED_CONFIGS:
        dst = os.path.join(job_dir(job.id), f"ds_{job.deepspeed}.json")
        os.makedirs(job_dir(job.id), exist_ok=True)
        with open(dst, "w", encoding="utf-8") as f:
            json.dump(DEEPSPEED_CONFIGS[job.deepspeed], f, ensure_ascii=False, indent=2)
        d["deepspeed"] = dst
    elif job.deepspeed == "fsdp":
        # PyTorch FSDP（HF TrainingArguments 的 FSDP 字段，torchrun 兼容）。
        # 核心项内置；其余高级项（fsdp_state_dict_type / fsdp_offload_params* /
        # fsdp_sharding_strategy 等）经 hyperparams/extra_args「未知键透传」注入，
        # 避免硬编码镜像 LF 版本可能不支持的键触发 HfArgumentParser「未使用」报错。
        d["fsdp"] = ("full_shard auto_wrap"
                     if hp.get("fsdp_transformer_layer_cls_to_wrap") else "full_shard")
        d["fsdp_use_orig_params"] = True
        if hp.get("fsdp_transformer_layer_cls_to_wrap"):
            d["fsdp_transformer_layer_cls_to_wrap"] = str(hp["fsdp_transformer_layer_cls_to_wrap"])

    # 内部键最后兜底（防止 extra_args/透传覆盖平台装配值）
    d.update({
        "model_name_or_path": base.path if base else "",
        "stage": stage,
        "output_dir": f"{job_dir(job.id)}/ckpt",
        "logging_dir": f"{job_dir(job.id)}/runs",
        "report_to": d.get("report_to", "tensorboard"),
        "trust_remote_code": True,
        "ddp_timeout": 180000000,
        "overwrite_output_dir": True,
        "plot_loss": True,
    })
    # 剥离镜像 LF 0.9.3 不支持的键（旧 hyperparams / extra_args 兜底）
    for k in UNSUPPORTED_LF_KEYS:
        d.pop(k, None)
    # export：剥离全部训练专用键——必须在「内部键兜底」之后（兜底会重塞
    # output_dir/logging_dir/report_to/ddp_timeout/overwrite_output_dir）
    if stage == "export":
        for k in EXPORT_STRIP_KEYS:
            d.pop(k, None)
    return {k: v for k, v in d.items() if v is not None}


def write_config(job: TrainingJob, db) -> str:
    d = build_config(job, db)
    # 官方镜像未装 tensorboard：依赖它的上报自动降级为 none（装过 tensorboard 的
    # 自构镜像保持原上报；用户显式选 wandb/mlflow 等其他集成不受影响）
    # 探测入参走运行时 pin：引擎升级后新镜像的 tensorboard 可用性按新镜像判定
    if d.get("report_to") in ("tensorboard", "all") \
            and not image_has_tensorboard(upgrade_service.current_image("llamafactory")):
        d["report_to"] = "none"
    cfg = os.path.join(job_dir(job.id), "config.yaml")
    os.makedirs(job_dir(job.id), exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as f:
        yaml.safe_dump(d, f, allow_unicode=True)
    return cfg


def launch(job: TrainingJob) -> None:
    """创建任务容器并 exec 启动训练（单机单卡 / 单机多卡 / 多机多卡）。

    - 单机单卡/export：单容器 + 原生 llamafactory-cli 单进程（与旧版字节级一致）
    - 单机多卡/多机多卡：每节点一个任务容器（共享 /data，各自独占 nproc 张卡），
      rendezvous 参数经 exec env 注入，镜像内 launcher 自启 torchrun；
      rank0 为主节点，MASTER_ADDR 取其容器 IP（容器网络互达）
    """
    # 单副本（object 型后端）：基座模型桶内为唯一副本，本地缓存缺失先拉取；
    # 失败抛异常 → 调度器把任务置 failed（不能拿不存在的路径起训练容器）
    from . import model_service
    db = SessionLocal()
    try:
        base = db.get(ModelAsset, job.base_model_id) if job.base_model_id else None
        if base and not model_service.ensure_model_local(base, db):
            raise RuntimeError(
                f"模型 {base.name} 不可用：本地不存在且对象存储拉取失败"
                "（object 型后端桶内为唯一副本，请确认模型已上传成功）")
        cfg = write_config(job, db)
    finally:
        db.close()

    nnodes = max(1, int(job.node_count or 1))
    nproc = max(1, int(job.nproc_per_node or 1))
    port = master_port(job)
    image = upgrade_service.current_image("llamafactory")
    env = container_env(job)
    names = rank_container_names(job)

    # 清理上一轮残留的 rank 容器
    for name in names:
        tc.remove_container(name, force=True)

    # 逐 rank 建占位容器（rank0 先建，便于取 MASTER_ADDR；镜像懒加载在此按 rank0 门控）
    cids = []
    for r, name in enumerate(names):
        cid = tc.create_container(
            name=name, image=image, gpus=nproc,
            cpus=(nproc * 4) or 4, mem_gb=24.0, env=env,
            labels={"fintune.job": str(job.id), "fintune.type": "training",
                    "fintune.rank": str(r)},
        )
        cids.append(cid)

    master_addr = resolve_master_addr(cids, nnodes)

    # 每容器 exec 拉起对应 rank 的启动命令；多卡/多机的 rendezvous 参数
    # （NNODES/NODE_RANK/NPROC_PER_NODE/MASTER_ADDR/MASTER_PORT）经 exec 级 env 注入，
    # 由镜像内 launcher 自启 torchrun（export/单卡不注入，单进程路径）
    keys = rank_task_keys(job)
    logs = rank_log_paths(job)
    for r in range(nnodes):
        tc.start_task(keys[r], cids[r],
                      training_cmd(job, cfg, r, nnodes, nproc, master_addr, port),
                      logs[r], env=rank_launch_env(job, r, master_addr, port))

    db = SessionLocal()
    try:
        j = db.get(TrainingJob, job.id)
        j.container_id = cids[0]
        j.container_name = names[0]
        j.config_path = cfg
        j.output_dir = f"{job_dir(job.id)}/ckpt"
        j.tensorboard_dir = f"{job_dir(job.id)}/runs"
        j.status = "running"
        j.error = ""  # 清除排队期"镜像下载中"等残留提示
        from datetime import datetime
        j.started_at = datetime.utcnow()
        db.commit()
    finally:
        db.close()


def cancel(job: TrainingJob) -> None:
    # 杀掉各 rank 容器内训练进程（torchrun/llamafactory），随后清理全部 rank 容器
    for name in rank_container_names(job):
        if tc.container_alive(name):
            try:
                tc.exec_run(name, ["pkill", "-9", "-f", "llamafactory"], timeout=30)
            except Exception:
                pass
        tc.remove_container(name, force=True)


def checkpoints(job: TrainingJob) -> list[dict]:
    """任务目录下可用 checkpoint 列表（step 降序）。

    只统计不早于任务创建时间的 checkpoint：任务 ID 虽单调递增，但数据库重建后
    /data 目录仍持久——新一代任务可能与旧代同号任务的残留 ckpt 目录碰撞
    （实测：re-clone 后 job 101 命中 11-19 旧代训练残留 checkpoint-5，
    把假 checkpoint 报给前端并允许"续训"是错误语义）。"""
    from datetime import timezone as _tz
    out = []
    base = f"{job_dir(job.id)}/ckpt"
    since = 0.0
    if job.created_at:
        ca = job.created_at
        if ca.tzinfo is None:
            ca = ca.replace(tzinfo=_tz.utc)  # DB 存 naive UTC（datetime.utcnow）
        since = ca.timestamp() - 5.0         # 5s 时钟抖动容差
    for p in glob.glob(os.path.join(base, "checkpoint-*")):
        step = p.rsplit("-", 1)[-1]
        try:
            step = int(step)
        except ValueError:
            continue
        try:
            if os.path.getmtime(p) < since:
                continue
        except OSError:
            continue
        out.append({"path": p, "step": step})
    return sorted(out, key=lambda x: x["step"], reverse=True)


def dir_size(path: str) -> int:
    """目录递归大小（字节）。注册产物时立即计量，不等调度器惰性回填。"""
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total


def register_artifact(job: TrainingJob) -> ModelAsset | None:
    """训练成功后注册 LoRA/合并产物到模型仓库（含血缘）"""
    db = SessionLocal()
    try:
        if job.stage == "export":
            path = f"{job_dir(job.id)}/merged"
            mtype = "merged"
            parent_id = job.adapter_asset_id
            name_base = f"merged-{job.name}"
        else:
            ckpts = checkpoints(job)
            if not ckpts:
                return None
            path = ckpts[0]["path"]
            # 产物类型按微调方式区分：LoRA 检查点才是 adapter；全参/freeze 检查点
            # 是完整模型，注册为 base 类（推理侧仅 lora 型走 vLLM adapter 路径，
            # 误标 lora 会导致部署时加载 adapter_config.json 失败）
            lora = job.finetuning_type == "lora"
            mtype = "lora" if lora else "base"
            parent_id = job.base_model_id
            name_base = f"{'lora' if lora else 'full'}-{job.name}-s{ckpts[0]['step']}"
        if not os.path.exists(path):
            return None
        base_asset = db.get(ModelAsset, job.base_model_id) if job.base_model_id else None
        name = name_base
        i = 1
        while db.query(ModelAsset).filter_by(project_id=job.project_id, name=name).first():
            i += 1
            name = f"{name_base}-{i}"
        asset = ModelAsset(
            project_id=job.project_id, name=name, type=mtype, path=path,
            parent_asset_id=parent_id, job_id=job.id,
            dataset_version_ids=job.dataset_version_ids or [],
            source=base_asset.name if base_asset else "",
            note=f"stage={job.stage}, finetune={job.finetuning_type}, auto-registered",
            created_by=job.submitted_by,
            size_bytes=dir_size(path),
        )
        db.add(asset)
        db.commit()
        db.refresh(asset)
        return asset
    except Exception:
        db.rollback()
        return None
    finally:
        db.close()

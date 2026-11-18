"""模型压缩（量化）服务：任务容器内执行 llamafactory-cli export 产出量化产物

仅支持量化；流程 = 选模型 → 选量化方式 → 创建任务 → 轮询 → 日志 → 产物入库可部署。

实现：LF 0.9.3 的 export 以 quantization_bit/method 加载模型（量化态）后 save_pretrained 写出量化权重。
训练镜像未预装量化后端（bitsandbytes/gptq/autoawq），任务启动时在容器内 pip 安装（阿里云镜像源）。
"""
import os

import yaml

from ..config import settings
from ..database import SessionLocal
from ..models import QuantTask, ModelAsset
from . import taskcontainer as tc
from . import upgrade_service

METHODS: dict[str, dict] = {
    "bnb4":  {"bit": 4, "method": "bitsandbytes", "pip": "bitsandbytes",     "device": "cpu",  "label": "BitsAndBytes NF4（4-bit）"},
    "bnb8":  {"bit": 8, "method": "bitsandbytes", "pip": "bitsandbytes",     "device": "cpu",  "label": "BitsAndBytes INT8（8-bit）"},
    "gptq4": {"bit": 4, "method": "gptq",         "pip": "gptqmodel optimum", "device": "auto", "label": "GPTQ（4-bit）"},
    "awq4":  {"bit": 4, "method": "awq",          "pip": "autoawq",          "device": "auto", "label": "AWQ（4-bit）"},
}


def task_dir(tid: int) -> str:
    return os.path.join(settings.data_dir, "quant", str(tid))


def build_config(task: QuantTask, model_path: str) -> str:
    m = METHODS[task.method]
    d = {
        "model_name_or_path": model_path,
        "stage": "pt",
        "do_train": False,
        "quantization_bit": m["bit"],
        "quantization_method": m["method"],
        "export_dir": f"{task_dir(task.id)}/out",
        "export_size": 5,
        "export_device": m["device"],
        "trust_remote_code": True,
    }
    cfg = os.path.join(task_dir(task.id), "config.yaml")
    os.makedirs(task_dir(task.id), exist_ok=True)
    with open(cfg, "w", encoding="utf-8") as f:
        yaml.safe_dump(d, f, allow_unicode=True)
    return cfg


def launch(task: QuantTask) -> None:
    db = SessionLocal()
    try:
        asset = db.get(ModelAsset, task.model_asset_id)
        cfg = build_config(task, asset.path)
    finally:
        db.close()
    name = f"ft-quant-{task.id}"
    tc.remove_container(name, force=True)
    cid = tc.create_container(
        name=name, image=upgrade_service.current_image("llamafactory"),
        gpus=task.gpu_count or 1, cpus=4, mem_gb=24.0,
        labels={"fintune.task": str(task.id), "fintune.type": "quantize"},
    )
    m = METHODS[task.method]
    pip_step = f"pip install -q {m['pip']} && " if m.get("pip") else ""
    cmd = ["bash", "-lc", f"cd /data && {pip_step} llamafactory-cli export {cfg} 2>&1"]
    tc.start_task(f"quant-{task.id}", cid, cmd, os.path.join(task_dir(task.id), "run.log"))
    db = SessionLocal()
    try:
        t = db.get(QuantTask, task.id)
        t.container_id = cid
        t.container_name = name
        t.work_dir = task_dir(task.id)
        t.output_dir = f"{task_dir(task.id)}/out"
        t.status = "running"
        t.error = ""  # 清除排队期"镜像下载中"等残留提示
        db.commit()
    finally:
        db.close()


def cleanup(task: QuantTask) -> None:
    """任务终态后清理容器（产物目录保留，供产物注册/排查）"""
    try:
        tc.remove_container(task.container_name, force=True)
    except Exception:
        pass


def register_artifact(task: QuantTask) -> ModelAsset | None:
    """量化成功后把产物注册进模型仓库（merged 类型，可直接部署）"""
    db = SessionLocal()
    try:
        path = f"{task_dir(task.id)}/out"
        if not os.path.exists(path) or not os.listdir(path):
            return None
        src = db.get(ModelAsset, task.model_asset_id)
        base = f"quant-{METHODS[task.method]['bit']}b-{src.name}" if src else f"quant-{task.name}"
        name = base
        i = 1
        while db.query(ModelAsset).filter_by(project_id=task.project_id, name=name).first():
            i += 1
            name = f"{base}-{i}"
        asset = ModelAsset(
            project_id=task.project_id, name=name, type="merged", path=path,
            parent_asset_id=task.model_asset_id,
            source=src.name if src else "",
            note=f"quantize method={task.method}, auto-registered",
            created_by=task.created_by,
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

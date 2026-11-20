"""模型资产存储位置服务：object 型后端模型单副本存桶，用时自动拉取本地

单副本语义（与数据集一致）：
- 模型选了 object 型存储后端 → 唯一权威副本在桶内，本地模型仓库路径
  （{data_dir}/models/hub/{name} 或 path 型挂载点下）是**服务缓存**；
- 训练/推理启动前经 ensure_model_local 检查本地，缺失则从桶拉回（get_dir），
  拉取失败必须让任务失败（不能拿不存在的路径去起容器）；
- HF/ModelScope 纳管的下载由 scheduler 负责，本模块不介入。
"""
import os
import logging

log = logging.getLogger("model_service")


def model_backend(asset, db):
    """模型的存储后端行（未选择/未传 db/内置(0)/后端已删除 → None，按平台默认处理）"""
    if asset is None or db is None or not getattr(asset, "storage_backend_id", None):
        return None
    if asset.storage_backend_id == 0:
        return None  # 内置 rustfs：无后端行，桶配置取 settings 主存储
    from ..models import StorageBackend
    return db.get(StorageBackend, asset.storage_backend_id)


def ensure_model_local(asset, db) -> bool:
    """保证模型目录本地可用：object 型（含内置 rustfs，id=0）且本地缺失 → 从桶下载
    （单副本拉取）。

    返回 True = 本地可用（已存在或拉取成功）；False = 不可用
    （object 型拉取失败/桶内无对象，或非 object 型且本地不存在——
    后者如 pending_download 的 HF 模型，由 scheduler 下载，调用方应等待或报错）。
    """
    if asset is None or not asset.path:
        return False
    if os.path.exists(asset.path):
        return True
    from . import storage
    kind, cfg, _ = storage.location(getattr(asset, "storage_backend_id", None), db)
    if kind != "object":
        return False
    try:
        n = storage.get_dir(asset.path, cfg)
    except Exception as e:
        log.error("模型 %s 桶内拉取失败: %s", asset.name, e)
        return False
    ok = n > 0 and os.path.exists(asset.path)
    if ok:
        log.info("模型 %s 已从对象存储拉取本地（%d 个文件）", asset.name, n)
    else:
        log.warning("模型 %s 桶内无对象（尚未上传成功？），无法本地拉取", asset.name)
    return ok

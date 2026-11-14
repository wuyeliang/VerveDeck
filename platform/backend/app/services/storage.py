"""对象存储层：rustfs（S3 兼容）+ 本地缓存

设计约定：
- 数据集版本文件（data.jsonl / raw.md）主存储在 rustfs 对象存储；
  本地 /data/datasets 是**处理缓存**（训练 symlink、MinerU 解析、清洗/合成都需要本地路径）。
- 写：先落本地缓存，再同步上传 S3（_create_version 统一收口）。
- 读：本地缓存命中直接用；未命中从 S3 下载后复用（ensure_local）。
- S3 未配置（settings.s3_endpoint 为空）时自动降级为纯本地存储，全部调用点无感。
- S3 key 与本地路径一一对应：{data_dir} 相对路径（datasets/p{pid}/{name}/v{N}/...）。
"""
import os
import logging

from ..config import settings
from ..database import SessionLocal
from ..models import StorageQueue

log = logging.getLogger("storage")

_client = None
_bucket_ready = False


def record(op: str, key: str, local_path: str, ok: bool, err: str = "") -> None:
    """对象存储同步留痕（同步队列）：每个上传/下载/删除操作一条记录。

    在后台线程中调用，独立短会话；写库失败仅告警，绝不影响主流程。
    """
    try:
        db = SessionLocal()
        try:
            db.add(StorageQueue(op=op, key=key[:500], local_path=local_path[:500],
                                status="done" if ok else "failed", error=(err or "")[:1000]))
            db.commit()
        finally:
            db.close()
    except Exception as e:
        log.warning("storage queue record failed: %s", e)


def enabled() -> bool:
    return bool(settings.s3_endpoint)


def client():
    """惰性初始化 minio 客户端（兼容 rustfs/minio/任意 S3 实现）

    注意：minio 库签名为 Minio(endpoint, access_key, secret_key, secure)，
    endpoint 为 "host[:port]" 字符串，无独立 port 参数。
    """
    global _client
    if _client is None:
        from minio import Minio
        url = settings.s3_endpoint
        if "://" not in url:
            url = "http://" + url
        secure = url.startswith("https")
        host = url.split("//")[-1].split("/")[0]
        _client = Minio(
            host,
            access_key=settings.s3_access_key,
            secret_key=settings.s3_secret_key,
            secure=secure,
        )
    return _client


def ensure_bucket() -> None:
    global _bucket_ready
    if not enabled() or _bucket_ready:
        return
    c = client()
    if not c.bucket_exists(settings.s3_bucket):
        c.make_bucket(settings.s3_bucket)
        log.info("created bucket %s", settings.s3_bucket)
    _bucket_ready = True


def _key(local_path: str) -> str:
    """本地绝对路径 -> S3 key（相对 data_dir）"""
    rel = os.path.relpath(local_path, settings.data_dir)
    if rel.startswith(".."):
        raise ValueError(f"path outside data_dir: {local_path}")
    return rel.replace(os.sep, "/")


def put_file(local_path: str) -> bool:
    """上传单个文件到 S3（失败不抛异常，仅告警：本地缓存仍完整可用）"""
    if not enabled() or not os.path.exists(local_path):
        return False
    try:
        ensure_bucket()
        c = client()
        key = _key(local_path)
        stat = os.stat(local_path)
        c.fput_object(settings.s3_bucket, key, local_path)
        log.info("s3 upload %s (%d bytes)", key, stat.st_size)
        record("put", key, local_path, True)
        return True
    except Exception as e:
        log.warning("s3 upload %s failed: %s", local_path, e)
        try:
            record("put", _key(local_path), local_path, False, str(e))
        except Exception:
            pass
        return False


def put_dir(local_dir: str) -> int:
    """上传目录下全部文件（版本目录：data.jsonl + raw.md 等），返回成功数"""
    if not enabled() or not os.path.isdir(local_dir):
        return 0
    n = 0
    for root, _, files in os.walk(local_dir):
        for f in files:
            p = os.path.join(root, f)
            if put_file(p):
                n += 1
    return n


def ensure_local(local_path: str) -> bool:
    """确保本地缓存存在：未命中则从 S3 下载。返回 True 表示文件可用。"""
    if os.path.exists(local_path):
        return True
    if not enabled():
        return os.path.exists(local_path)
    try:
        ensure_bucket()
        c = client()
        key = _key(local_path)
        if not c.stat_object(settings.s3_bucket, key):
            return False
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        c.fget_object(settings.s3_bucket, key, local_path)
        log.info("s3 download %s -> %s", key, local_path)
        record("get", key, local_path, True)
        return True
    except Exception as e:
        log.warning("s3 download %s failed: %s", local_path, e)
        try:
            record("get", _key(local_path), local_path, False, str(e))
        except Exception:
            pass
        return False


def delete_dir(local_dir: str) -> None:
    """删除 S3 上对应前缀（数据集删除时调用，失败仅告警）"""
    if not enabled() or not os.path.isdir(local_dir):
        return
    prefix = ""
    try:
        prefix = _key(local_dir.rstrip(os.sep)) + "/"
    except ValueError:
        return
    ok, err, n = False, "", 0
    try:
        ensure_bucket()
        c = client()
        for o in c.list_objects(settings.s3_bucket, prefix=prefix, recursive=True):
            c.remove_object(settings.s3_bucket, o.object_name)
            n += 1
        ok = True
        log.info("s3 deleted prefix %s (%d objects)", prefix, n)
    except Exception as e:
        err = str(e)
        log.warning("s3 delete prefix %s failed: %s", local_dir, e)
    record("delete", prefix, local_dir, ok, err if not ok else f"{n} objects")


def health() -> dict:
    """对象存储健康状态（dashboard / 健康检查用）"""
    if not enabled():
        return {"enabled": False, "endpoint": "", "bucket": settings.s3_bucket, "ok": True, "detail": "S3 未启用（纯本地存储）"}
    try:
        ensure_bucket()
        return {"enabled": True, "endpoint": settings.s3_endpoint, "bucket": settings.s3_bucket, "ok": True}
    except Exception as e:
        return {"enabled": True, "endpoint": settings.s3_endpoint, "bucket": settings.s3_bucket, "ok": False, "detail": str(e)[:200]}


def list_objects(prefix: str = "", limit: int = 200) -> list[dict]:
    """浏览对象（模拟目录：delimiter=/ 时前缀聚合为「文件夹」条目）"""
    if not enabled():
        return []
    ensure_bucket()
    c = client()
    out = []
    for o in c.list_objects(settings.s3_bucket, prefix=prefix, delimiter="/", recursive=False):
        if o.is_dir:
            out.append({"name": o.object_name, "is_dir": True, "size": 0, "last_modified": ""})
        else:
            out.append({"name": o.object_name, "is_dir": False,
                        "size": o.size or 0,
                        "last_modified": str(o.last_modified) if o.last_modified else ""})
        if len(out) >= max(1, min(int(limit), 500)):
            break
    return out


def delete_prefix(prefix: str, cap: int = 5000) -> int:
    """删除前缀下全部对象（管理页手动删除用），返回删除数；超 cap 抛异常防误删"""
    if not enabled():
        return 0
    ensure_bucket()
    c = client()
    deleted = 0
    for o in c.list_objects(settings.s3_bucket, prefix=prefix, recursive=True):
        c.remove_object(settings.s3_bucket, o.object_name)
        deleted += 1
        if deleted > cap:
            raise RuntimeError(f"prefix 下对象超过 {cap} 个，已中止（防误删）")
    record("delete", prefix, "", True, f"{deleted} objects (manual)")
    return deleted


def stats() -> dict:
    """桶统计：对象数 / 总字节（上限 1 万，超出只标注截断）"""
    if not enabled():
        return {"objects": 0, "bytes": 0, "truncated": False}
    ensure_bucket()
    c = client()
    n, total, truncated = 0, 0, False
    for o in c.list_objects(settings.s3_bucket, recursive=True):
        n += 1
        total += o.size or 0
        if n >= 10000:
            truncated = True
            break
    return {"objects": n, "bytes": total, "truncated": truncated}

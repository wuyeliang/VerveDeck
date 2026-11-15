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

log = logging.getLogger("storage")

_client = None
_bucket_ready = False


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
        return True
    except Exception as e:
        log.warning("s3 upload %s failed: %s", local_path, e)
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
        return True
    except Exception as e:
        log.warning("s3 download %s failed: %s", local_path, e)
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
    n = 0
    try:
        ensure_bucket()
        c = client()
        for o in c.list_objects(settings.s3_bucket, prefix=prefix, recursive=True):
            c.remove_object(settings.s3_bucket, o.object_name)
            n += 1
        log.info("s3 deleted prefix %s (%d objects)", prefix, n)
    except Exception as e:
        log.warning("s3 delete prefix %s failed: %s", local_dir, e)


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
    """浏览对象（模拟目录：minio-py recursive=False 即按前缀聚合出「文件夹」条目）"""
    if not enabled():
        return []
    ensure_bucket()
    c = client()
    out = []
    for o in c.list_objects(settings.s3_bucket, prefix=prefix, recursive=False):
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


# ---------------------------------------------------------------------------
# 多类型存储后端注册表：object（S3 兼容）/ local / nfs / block
# ---------------------------------------------------------------------------

def _minio_client(endpoint: str, access_key: str, secret_key: str):
    from minio import Minio
    url = endpoint or ""
    if "://" not in url:
        url = "http://" + url
    secure = url.startswith("https")
    host = url.split("//")[-1].split("/")[0]
    return Minio(host, access_key=access_key, secret_key=secret_key, secure=secure)


def probe_object(cfg: dict) -> dict:
    """对象存储连通性测试：桶可达 + 统计（10 万对象截断）"""
    cfg = cfg or {}
    bucket = cfg.get("bucket", "")
    try:
        c = _minio_client(cfg.get("endpoint", ""), cfg.get("access_key", ""), cfg.get("secret_key", ""))
        if not c.bucket_exists(bucket):
            return {"ok": False, "detail": f"bucket {bucket} 不存在或不可访问"}
        n, total = 0, 0
        for o in c.list_objects(bucket, recursive=True):
            n += 1
            total += o.size or 0
            if n >= 100000:
                break
        return {"ok": True, "detail": "连接成功", "objects": n, "bytes": total}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


def probe_path(path: str) -> dict:
    """local/nfs/block 探测：把宿主目录的**父目录**只读挂进一次性容器跑 df。

    容器内只能访问挂载点 /probe，无法直接引用宿主路径；挂父目录后检查
    /probe/<basename> 是否存在，可正确识别路径不存在（直接挂目标路径会被
    docker 自动补建空目录，误报 ok）。顺带 df 拿到宿主文件系统容量。
    显式 create/start/wait/logs/remove（docker-py 7.x 的 containers.run
    在 detach=False 时返回 logs bytes 而非容器对象，且 remove=False 会
    残留容器，故不走 run 快捷方法）。
    """
    import docker
    path = (path or "").strip().rstrip("/")
    if not path.startswith("/"):
        return {"ok": False, "detail": "path 需为绝对路径", "size_used": 0, "size_total": 0}
    parent = os.path.dirname(path) or "/"
    target = "/probe/" + os.path.basename(path)
    try:
        d = docker.from_env()
        probe_img = "fintune/web:1.0"
        try:
            d.images.get(probe_img)
        except Exception:
            probe_img = "alpine:3.20"
        ctr = d.containers.create(
            probe_img,
            entrypoint=["sh", "-c", f"test -d {target!r} && df -kP {target!r} | tail -1"],
            volumes={parent: {"bind": "/probe", "mode": "ro"}},
        )
        try:
            ctr.start()
            res = ctr.wait(timeout=60)
            out = ctr.logs().decode("utf-8", "ignore").strip()
        finally:
            ctr.remove(force=True)
        if res.get("StatusCode", 0) != 0 or not out or "No such file" in out:
            return {"ok": False, "detail": f"路径 {path} 不存在或不可读"}
        p = out.split()
        # df -kP: fs blocks(1K) used available capacity mountpoint
        if len(p) >= 6:
            total = int(p[1]) * 1024
            avail = int(p[3]) * 1024
            return {"ok": True, "detail": "ok",
                    "size_total": total, "size_used": max(0, total - avail)}
        return {"ok": True, "detail": out[:160]}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200]}


def test_backend(btype: str, cfg: dict) -> dict:
    """统一入口：按类型分发探测，返回 {ok, detail, size_used, size_total, objects?, bytes?}"""
    if btype == "object":
        r = probe_object(cfg)
        r.setdefault("size_used", 0)
        r.setdefault("size_total", 0)
        return r
    path = (cfg or {}).get("path", "")
    if not path:
        return {"ok": False, "detail": "path 必填", "size_used": 0, "size_total": 0}
    r = probe_path(path)
    r.setdefault("size_used", 0)
    r.setdefault("size_total", 0)
    return r


def list_objects_cfg(cfg: dict, prefix: str = "", limit: int = 200) -> list[dict]:
    """按指定对象存储配置浏览（多后端浏览用，recursive=False 按目录聚合）"""
    cfg = cfg or {}
    c = _minio_client(cfg.get("endpoint", ""), cfg.get("access_key", ""), cfg.get("secret_key", ""))
    bucket = cfg.get("bucket", "")
    out = []
    for o in c.list_objects(bucket, prefix=prefix, recursive=False):
        if o.is_dir:
            out.append({"name": o.object_name, "is_dir": True, "size": 0, "last_modified": ""})
        else:
            out.append({"name": o.object_name, "is_dir": False,
                        "size": o.size or 0,
                        "last_modified": str(o.last_modified) if o.last_modified else ""})
        if len(out) >= max(1, min(int(limit), 500)):
            break
    return out


def delete_prefix_cfg(cfg: dict, prefix: str, cap: int = 5000) -> int:
    """按指定对象存储配置删前缀（防误删上限同 delete_prefix）"""
    cfg = cfg or {}
    c = _minio_client(cfg.get("endpoint", ""), cfg.get("access_key", ""), cfg.get("secret_key", ""))
    bucket = cfg.get("bucket", "")
    deleted = 0
    for o in c.list_objects(bucket, prefix=prefix, recursive=True):
        c.remove_object(bucket, o.object_name)
        deleted += 1
        if deleted > cap:
            raise RuntimeError(f"prefix 下对象超过 {cap} 个，已中止（防误删）")
    return deleted

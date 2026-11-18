"""对象存储层：rustfs（S3 兼容）+ 本地缓存 + 多存储位置

设计约定：
- 数据集版本文件（data.jsonl / raw.md）主存储在 rustfs 对象存储；
  本地 /data/datasets 是**处理缓存**（训练 symlink、MinerU 解析、清洗/合成都需要本地路径）。
- 写：先落本地缓存，再同步上传 S3（_create_version 统一收口）。
- 读：本地缓存命中直接用；未命中从 S3 下载后复用（ensure_local）。
- S3 未配置（settings.s3_endpoint 为空）时自动降级为纯本地存储，全部调用点无感。
- S3 key 与本地路径一一对应：{data_dir} 相对路径（datasets/p{pid}/{name}/v{N}/...）。

存储位置选择（数据集/模型可选注册后端）：
- object 型后端：本地工作目录不变，put/ensure/delete 通过 cfg 参数指向该桶
  （cfg=None 时走平台主对象存储，行为与历史完全一致）。
- path 型后端（local/nfs/block/juicefs）：文件直接建在挂载点下（backend_subdir），
  读写即本地、无同步步骤；juicefs 的 FUSE 挂载点在宿主可见，backend 与 worker 容器
  经 /data（或同路径 bind）直接访问。
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


def _bucket_for(cfg: dict | None):
    """解析对象存储目标：cfg=None → 平台主对象存储（settings）；否则注册后端的对象存储。

    返回 (client, bucket)；主存储未启用时返回 (None, None)（调用点静默降级纯本地）。
    """
    if cfg:
        bucket = cfg.get("bucket", "")
        if not bucket:
            raise ValueError("对象存储配置缺 bucket")
        c = _minio_client(cfg.get("endpoint", ""), cfg.get("access_key", ""), cfg.get("secret_key", ""))
        if not c.bucket_exists(bucket):
            c.make_bucket(bucket)
        return c, bucket
    if not enabled():
        return None, None
    ensure_bucket()
    return client(), settings.s3_bucket


def put_file(local_path: str, cfg: dict | None = None) -> bool:
    """上传单个文件到对象存储（主存储或 cfg 指定后端）。失败不抛异常，仅告警：本地缓存仍完整可用"""
    try:
        c, bucket = _bucket_for(cfg)
    except Exception as e:
        log.warning("s3 resolve bucket failed: %s", e)
        return False
    if c is None or not os.path.exists(local_path):
        return False
    try:
        key = _key(local_path)
        stat = os.stat(local_path)
        c.fput_object(bucket, key, local_path)
        log.info("s3 upload %s (%d bytes) -> %s", key, stat.st_size, bucket)
        return True
    except Exception as e:
        log.warning("s3 upload %s failed: %s", local_path, e)
        return False


def put_dir(local_dir: str, cfg: dict | None = None) -> int:
    """上传目录下全部文件（版本目录：data.jsonl + raw.md 等），返回成功数"""
    if not os.path.isdir(local_dir):
        return 0
    n = 0
    for root, _, files in os.walk(local_dir):
        for f in files:
            if put_file(os.path.join(root, f), cfg):
                n += 1
    return n


def ensure_local(local_path: str, cfg: dict | None = None) -> bool:
    """确保本地缓存存在：未命中则从对象存储下载。返回 True 表示文件可用。"""
    if os.path.exists(local_path):
        return True
    try:
        c, bucket = _bucket_for(cfg)
    except Exception as e:
        log.warning("s3 resolve bucket failed: %s", e)
        return False
    if c is None:
        return False
    try:
        key = _key(local_path)
        if not c.stat_object(bucket, key):
            return False
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        c.fget_object(bucket, key, local_path)
        log.info("s3 download %s -> %s (from %s)", key, local_path, bucket)
        return True
    except Exception as e:
        log.warning("s3 download %s failed: %s", local_path, e)
        return False


def delete_dir(local_dir: str, cfg: dict | None = None) -> None:
    """删除对象存储上对应前缀（数据集删除时调用，失败仅告警）"""
    if not os.path.isdir(local_dir):
        return
    prefix = ""
    try:
        prefix = _key(local_dir.rstrip(os.sep)) + "/"
        c, bucket = _bucket_for(cfg)
    except Exception:
        return
    if c is None:
        return
    n = 0
    try:
        for o in c.list_objects(bucket, prefix=prefix, recursive=True):
            c.remove_object(bucket, o.object_name)
            n += 1
        log.info("s3 deleted prefix %s (%d objects) from %s", prefix, n, bucket)
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


def probe_juicefs(cfg: dict) -> dict:
    """juicefs 探测：挂载点必须是 FUSE 挂载（df -T 的 fstype 含 fuse），防止把普通目录误报为 juicefs。

    一次性容器**直接 bind 挂载点本身**（而非父目录）：容器 bind 默认 rprivate
    不传播宿主子挂载，父目录挂进去只能看到底层空目录，FUSE 挂载不可见；直接
    bind 挂载点路径则解析到 FUSE 挂载体本身，容器内可见。挂载点不存在时 docker
    会自动补建空目录（非 FUSE），由 fstype 校验正确判错，不会误报。
    """
    cfg = cfg or {}
    path = (cfg.get("path") or "").strip().rstrip("/")
    if not path.startswith("/"):
        return {"ok": False, "detail": "挂载点 path 需为绝对路径", "size_used": 0, "size_total": 0}
    import docker
    target = "/probe/juicefs"
    try:
        d = docker.from_env()
        probe_img = "fintune/web:1.0"
        try:
            d.images.get(probe_img)
        except Exception:
            probe_img = "alpine:3.20"
        ctr = d.containers.create(
            probe_img,
            entrypoint=["sh", "-c", f"test -d {target!r} && df -kPT {target!r} | tail -1"],
            volumes={path: {"bind": target, "mode": "ro"}},
        )
        try:
            ctr.start()
            res = ctr.wait(timeout=60)
            out = ctr.logs().decode("utf-8", "ignore").strip()
        finally:
            ctr.remove(force=True)
        if res.get("StatusCode", 0) != 0 or not out or "No such file" in out:
            return {"ok": False, "detail": f"挂载点 {path} 不存在或不可读", "size_used": 0, "size_total": 0}
        p = out.split()
        # df -kPT: Filesystem Type 1024-blocks Used Available Capacity Mounted-on
        if len(p) >= 7:
            fstype = p[1]
            if "fuse" not in fstype.lower():
                return {"ok": False,
                        "detail": f"{path} 不是 FUSE 挂载（fstype={fstype}），请先挂载 JuiceFS 卷",
                        "size_used": 0, "size_total": 0}
            total = int(p[2]) * 1024
            avail = int(p[4]) * 1024
            return {"ok": True, "detail": f"FUSE 挂载正常（fstype={fstype}）",
                    "size_total": total, "size_used": max(0, total - avail)}
        return {"ok": False, "detail": out[:160], "size_used": 0, "size_total": 0}
    except Exception as e:
        return {"ok": False, "detail": str(e)[:200], "size_used": 0, "size_total": 0}


def test_backend(btype: str, cfg: dict) -> dict:
    """统一入口：按类型分发探测，返回 {ok, detail, size_used, size_total, objects?, bytes?}"""
    if btype == "object":
        r = probe_object(cfg)
        r.setdefault("size_used", 0)
        r.setdefault("size_total", 0)
        return r
    if btype == "juicefs":
        r = probe_juicefs(cfg)
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


# ---------------------------------------------------------------------------
# 存储位置选择：数据集/模型可选已注册的存储后端
#   path 型（local/nfs/block/juicefs）：文件直接建在挂载点下（backend_subdir），无同步；
#   object 型：本地工作目录 + 同步到该桶（put/ensure/delete 传 cfg）；
#   未选择（None）：平台默认（本地 /data + 主对象存储 rustfs）。
# ---------------------------------------------------------------------------

PATH_BASED_TYPES = ("local", "nfs", "block", "juicefs")


def backend_root(backend) -> str:
    """path 型后端的宿主挂载/根目录（未配置返回空串）"""
    return ((backend.config or {}).get("path") or "").strip().rstrip("/")


def backend_subdir(backend, *subdirs: str) -> str:
    """path 型后端根目录下的子目录（如 datasets/p{pid}/{name}）"""
    root = backend_root(backend)
    if not root:
        raise ValueError(f"存储后端 #{backend.id} 未配置 path")
    return os.path.join(root, *subdirs)


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

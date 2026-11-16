"""集群时间服务：宿主时区读取/应用 + NTP 配置探测/写入 + SNTP 连通性探针。

宿主操作经一次性容器执行（backend 持 /var/run/docker.sock）：
- 只读探测：宿主 /etc 只读挂载；
- 时区应用：宿主 /etc 读写挂载，/etc/localtime 覆盖（容器 zoneinfo 拷贝）+ /etc/timezone 写入，立即生效；
- NTP 写入：按宿主实际安装的时钟同步守护进程（chrony/ntpd/systemd-timesyncd）改其配置文件，
  容器内无法重启宿主 systemd 服务，故由 API 返回重启命令（UI 展示）。
容器与宿主共享时钟，SNTP 探针直接在 backend 进程发 UDP 包即可测宿主时钟偏差。
一次性容器统一走显式 create/start/wait/remove（docker-py 7.x 的 containers.run
在 detach=False 时返回 logs bytes 而非容器对象，且 remove=False 会残留容器）。
"""
import base64
import logging
import os
import re
import socket
import struct
import time
from datetime import datetime

log = logging.getLogger("time")

PROBE_IMG = "fintune/web:1.0"
NTP_EPOCH = 2208988800  # NTP epoch(1900) 与 unix epoch(1970) 差值

# 宿主时钟同步守护进程识别表：(实现, 配置文件候选)
NTP_IMPLS = (
    ("chrony", ("/etc/chrony/chrony.conf", "/etc/chrony.conf")),
    ("ntpd", ("/etc/ntp.conf",)),
    ("systemd-timesyncd", ("/etc/systemd/timesyncd.conf",)),
)
NTP_RESTART = {
    "chrony": "systemctl restart chrony",
    "ntpd": "systemctl restart ntp",
    "systemd-timesyncd": "systemctl restart systemd-timesyncd",
}


def _docker():
    import docker
    d = docker.from_env()
    img = PROBE_IMG
    try:
        d.images.get(img)
    except Exception:
        img = "alpine:3.20"
    return d, img


def _run_host_cmd(d, img: str, cmd: str, volumes: dict, timeout: int = 60) -> tuple[int, str]:
    """一次性容器执行 cmd，返回 (exit_code, 合并日志)；finally 强制清理防残留"""
    ctr = d.containers.create(img, entrypoint=["sh", "-c", cmd], volumes=volumes)
    try:
        ctr.start()
        res = ctr.wait(timeout=timeout)
        out = ctr.logs().decode("utf-8", "ignore")
        return res.get("StatusCode", 0), out
    finally:
        ctr.remove(force=True)


def _read_host_etc(rel: str) -> str:
    """读宿主 /etc 下相对路径文件（只读挂载）"""
    d, img = _docker()
    _, out = _run_host_cmd(d, img, f"cat '/host-etc/{rel}' 2>/dev/null",
                           {"/etc": {"bind": "/host-etc", "mode": "ro"}})
    return out


def _write_host_etc(rel: str, content: str) -> bool:
    """写宿主 /etc 下相对路径文件（base64 传输避免 shell 转义）"""
    b64 = base64.b64encode(content.encode("utf-8")).decode("ascii")
    d, img = _docker()
    code, out = _run_host_cmd(d, img, f"echo {b64} | base64 -d > '/host-etc/{rel}' && echo WROTE",
                              {"/etc": {"bind": "/host-etc", "mode": "rw"}})
    if code != 0 or "WROTE" not in out:
        log.warning("write host /etc/%s failed: %s", rel, out.strip()[:200])
        return False
    return True


# ---------------------------------------------------------------------------
# 时区
# ---------------------------------------------------------------------------

def host_timezone() -> str:
    """读宿主当前 IANA 时区（/etc/localtime 符号链接目标，兜底 /etc/timezone）"""
    d, img = _docker()
    _, out = _run_host_cmd(d, img,
        'z=$(readlink /host-etc/localtime 2>/dev/null | sed "s|.*/zoneinfo/||"); '
        '[ -n "$z" ] || z=$(tr -d "[:space:]" < /host-etc/timezone 2>/dev/null); '
        'echo "${z:-Etc/UTC}"',
        {"/etc": {"bind": "/host-etc", "mode": "ro"}})
    lines = [l.strip() for l in out.splitlines() if l.strip()]
    return lines[-1] if lines else "Etc/UTC"


def valid_timezone(tz: str) -> bool:
    """IANA 时区名校验（后端容器 tzdata 存在即认；web/zoneinfo 同源 debian tzdata）"""
    if not tz or not re.fullmatch(r"[A-Za-z0-9_+\-./]+", tz):
        return False
    # isfile（非 exists）：拒绝 "Asia/" 这类指向目录/尾斜杠的伪时区名
    return os.path.isfile(os.path.join("/usr/share/zoneinfo", *tz.split("/")))


def apply_timezone(tz: str) -> dict:
    """应用时区到宿主：/etc/localtime 覆盖 + /etc/timezone 写入，回读校验"""
    if not valid_timezone(tz):
        return {"ok": False, "detail": f"时区 {tz} 不存在或非法"}
    d, img = _docker()
    code, out = _run_host_cmd(d, img,
        f"cp -f /usr/share/zoneinfo/{tz} /host-etc/localtime && "
        f"echo '{tz}' > /host-etc/timezone && echo APPLIED",
        {"/etc": {"bind": "/host-etc", "mode": "rw"}})
    if code != 0 or "APPLIED" not in out:
        return {"ok": False, "detail": (out.strip() or f"写入失败 exit={code}")[-200:]}
    cur = host_timezone()
    if cur == tz:
        log.info("host timezone applied: %s", tz)
        return {"ok": True, "detail": f"宿主时区已切换为 {tz}"}
    return {"ok": False, "detail": f"写入成功但回读不一致（当前 {cur}）"}


def host_now(tz: str = "") -> str:
    """宿主当前时间（容器与宿主共享时钟；按指定 IANA 时区格式化）"""
    try:
        from zoneinfo import ZoneInfo
        z = ZoneInfo(tz) if valid_timezone(tz) else ZoneInfo("Etc/UTC")
    except Exception:
        z = None
    if z is None:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return datetime.now(z).strftime("%Y-%m-%d %H:%M:%S %Z")


# ---------------------------------------------------------------------------
# NTP
# ---------------------------------------------------------------------------

def detect_ntp() -> dict:
    """探测宿主时钟同步守护进程：返回 {impl, file, configured}"""
    d, img = _docker()
    code, out = _run_host_cmd(d, img,
        'for f in /host-etc/chrony/chrony.conf /host-etc/chrony.conf '
        '/host-etc/ntp.conf /host-etc/systemd/timesyncd.conf; do '
        '[ -f "$f" ] && echo "$f"; done',
        {"/etc": {"bind": "/host-etc", "mode": "ro"}})
    present = [l.strip()[len("/host-etc/"):] for l in out.splitlines() if l.strip()]
    for impl, paths in NTP_IMPLS:
        for p in paths:
            if p[len("/etc/"):] in present:
                return {"impl": impl, "file": p,
                        "configured": _parse_ntp(impl, _read_host_etc(p[len("/etc/"):]))}
    return {"impl": "", "file": "", "configured": []}


def _parse_ntp(impl: str, text: str) -> list:
    """从配置文件解析当前生效的 NTP 服务器列表"""
    srvs = []
    for line in (text or "").splitlines():
        s = line.strip()
        if impl == "systemd-timesyncd":
            if s.startswith("NTP=") and not s.startswith("#"):
                srvs = [x for x in s[4:].split() if x]
                break
        elif s.startswith(("server ", "pool ")):
            srvs.append(s.split()[1])
    return srvs


def apply_ntp(servers: list) -> dict:
    """把 NTP 服务器列表写入宿主时钟同步配置。空列表=恢复系统默认。
    返回 {ok, impl, file, detail, restart_hint, configured_after}"""
    info = detect_ntp()
    impl, path = info.get("impl", ""), info.get("file", "")
    if not impl:
        return {"ok": False, "detail": "宿主未识别到时钟同步守护进程配置（chrony/ntpd/systemd-timesyncd）"}
    text = _read_host_etc(path[len("/etc/"):])
    rel = path[len("/etc/"):]

    if impl == "systemd-timesyncd":
        if servers:
            new_line = "NTP=" + " ".join(servers)
        else:
            new_line = "#NTP="  # 置空 = 跟随系统默认（含 DHCP 下发的 NTP）
        lines = text.splitlines()
        replaced = False
        for i, l in enumerate(lines):
            if l.strip().startswith("NTP=") or l.strip() == "#NTP=":
                lines[i] = new_line
                replaced = True
                break
        if not replaced:
            for i, l in enumerate(lines):
                if l.strip() == "[Time]":
                    lines.insert(i + 1, new_line)
                    break
            else:
                lines.append(new_line)
        content = "\n".join(lines) + "\n"
    else:
        # chrony / ntpd：删旧 server/pool 行，追加新 server 行
        lines = [l for l in text.splitlines() if not l.strip().startswith(("server ", "pool "))]
        for s in servers:
            lines.append(f"server {s}")
        content = "\n".join(lines).rstrip("\n") + "\n"

    if not _write_host_etc(rel, content):
        return {"ok": False, "impl": impl, "file": path,
                "detail": "配置写入失败（详见 backend 日志）",
                "restart_hint": NTP_RESTART.get(impl, ""), "configured_after": servers}
    log.info("host ntp config written (%s): %s", impl, servers or "(default)")
    detail = "NTP 配置已写入宿主" + (f"：{', '.join(servers)}" if servers else "（恢复系统默认）")
    return {"ok": True, "impl": impl, "file": path, "detail": detail,
            "restart_hint": NTP_RESTART.get(impl, ""),
            "configured_after": _parse_ntp(impl, content)}


def _sntp_probe(server: str, port: int, timeout: float) -> dict:
    """单次 SNTP 查询，返回 {server, ok, offset_ms?, detail?}"""
    label = f"{server}:{port}"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        try:
            pkt = b"\xff" + b"\0" * 47  # leap=3(version), stratum 0, mode 3(client)
            t1 = time.time()
            s.sendto(pkt, (server, port))
            data, _ = s.recvfrom(1024)
            t4 = time.time()
        finally:
            s.close()
        if len(data) < 48 or (data[0] >> 6) != 3:
            return {"server": label, "ok": False, "detail": "响应格式错误"}

        def ts(off: int) -> float:
            v = struct.unpack("!I", data[off:off + 4])[0]
            return v - NTP_EPOCH if v else 0.0

        t3 = ts(40)  # transmit
        if not t3:
            return {"server": label, "ok": False, "detail": "无 transmit 时间戳"}
        t2 = ts(32)  # receive
        offset = ((t2 - t1) + (t3 - t4)) / 2 if t2 else (t3 - (t1 + t4) / 2)
        return {"server": label, "ok": True, "offset_ms": round(offset * 1000, 1)}
    except socket.timeout:
        return {"server": label, "ok": False, "detail": "超时（出网 UDP 123 可能被防火墙拦截）"}
    except Exception as e:
        return {"server": label, "ok": False, "detail": str(e)[:120]}


def probe_ntp(servers: list, timeout: float = 3.0) -> list:
    """逐个探测 NTP 服务器连通性与时钟偏差（容器时钟=宿主时钟）"""
    out = []
    for s in list(servers)[:8]:
        host, port = s, 123
        if ":" in s:
            host, _, p = s.rpartition(":")
            if p.isdigit() and 1 <= int(p) <= 65535:
                port = int(p)
        out.append(_sntp_probe(host, port, timeout))
    return out

"""DVC 数据集版本控制（subprocess 调用 dvc/git CLI，仓库粒度 = 数据集目录）"""
import os
import subprocess


def _sh(cmd: list[str], cwd: str, timeout: int = 900) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except Exception as e:
        return 1, str(e)


def _git(repo: str, *args: str) -> tuple[int, str]:
    return _sh(["git", "-C", repo, *args], "/")


def init_repo(repo: str) -> None:
    os.makedirs(repo, exist_ok=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "fintune@local")
    _git(repo, "config", "user.name", "fintune")
    if not os.path.exists(os.path.join(repo, ".dvc")):
        _sh(["dvc", "init", "-q"], repo)
        remote = os.path.join("/data/dvc-remote", os.path.basename(os.path.normpath(repo)))
        os.makedirs(remote, exist_ok=True)
        _sh(["dvc", "remote", "add", "-d", "fintune-remote", remote], repo)
        _sh(["dvc", "config", "core.autostage", "true"], repo)
        # 忽略大文件缓存以外的临时物
        dvcgit = os.path.join(repo, ".dvc", ".gitignore")
        if os.path.exists(dvcgit):
            with open(dvcgit, "a") as f:
                f.write("\n*.tmp\n")


def add_and_commit(repo: str, msg: str) -> str:
    """dvc add 全部变更 + git add/commit + dvc commit，返回 dvc rev"""
    init_repo(repo)
    _sh(["dvc", "add", "-R", "."], repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg, "--allow-empty")
    _sh(["dvc", "commit", "-m", msg, "--no-cache"], repo)
    code, rev = _git(repo, "rev-parse", "HEAD")
    return rev.strip() if code == 0 else ""


def latest_rev(repo: str) -> str:
    code, rev = _git(repo, "rev-parse", "HEAD")
    return rev.strip() if code == 0 else ""


def log(repo: str, limit: int = 20) -> list[dict]:
    code, out = _git(repo, "log", f"--max-count={limit}", "--format=%h%x09%ad%x09%s", "--date=iso")
    items = []
    if code == 0:
        for line in out.splitlines():
            if "\t" in line:
                h, d, s = line.split("\t", 2)
                items.append({"rev": h, "date": d, "msg": s})
    return items


def push_remote(repo: str) -> tuple[bool, str]:
    """把版本数据推到 DVC remote（本地目录 remote）"""
    code, out = _sh(["dvc", "push"], repo)
    return code == 0, out

"""MinerU 文档解析服务（自封装，API 稳定可控；适配 MinerU 4.x CLI）
POST /parse  multipart: file
  -> {"markdown": "...", "pages": n, "files": [out files]}
  - 文本类文件（.md/.markdown/.txt）直通读取，不走解析引擎
  - 二进制文档（pdf/ofd/图片等）走 `mineru parse`（需本地 mineru server，启动时自动拉起）
POST /parse/text  {"text": "...", "lang": "ch"}  (纯文本直通，便于联调)
GET  /health
"""
import os, subprocess, shutil, tempfile, re
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse

app = FastAPI(title="MinerU Parse Service")

TEXT_EXTS = {".md", ".markdown", ".txt"}


def _ensure_server():
    """MinerU 4.x CLI 依赖本地 server 进程；容器启动时拉起（已运行则忽略报错）"""
    try:
        subprocess.run(["mineru", "server", "start"],
                       capture_output=True, text=True, timeout=600)
    except Exception as e:
        print(f"mineru server start failed: {e}")


@app.on_event("startup")
def _startup():
    _ensure_server()


def find_markdowns(outdir):
    mds = []
    if os.path.isfile(outdir):  # --output 可能直接指向单个 .md 文件
        if outdir.endswith(".md"):
            return [outdir]
        return mds
    for root, _, files in os.walk(outdir):
        for f in files:
            if f.endswith(".md"):
                mds.append(os.path.join(root, f))
    return sorted(mds)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/parse")
async def parse(file: UploadFile = File(...)):
    tmp = tempfile.mkdtemp(prefix="mineru_")
    raw = os.path.join(tmp, os.path.basename(file.filename) or "doc.pdf")
    with open(raw, "wb") as f:
        shutil.copyfileobj(file.file, f)
    outdir = os.path.join(tmp, "out")
    try:
        ext = os.path.splitext(raw)[1].lower()
        if ext in TEXT_EXTS:
            # MinerU 4.x：文本文件无需解析引擎，直接读取
            with open(raw, encoding="utf-8", errors="ignore") as fp:
                md = fp.read()
            pages = md.count("\f") or 1
            return {"markdown": md, "pages": pages,
                    "files": [os.path.basename(raw)]}
        p = subprocess.run(
            ["mineru", "parse", raw, "--pages", "all", "--output", outdir],
            capture_output=True, text=True, timeout=1800)
        mds = find_markdowns(outdir)
        if not mds:
            return JSONResponse(status_code=500, content={
                "error": "no markdown output",
                "stdout_tail": p.stdout[-2000:], "stderr_tail": p.stderr[-2000:]})
        parts = []
        for m in mds:
            with open(m, encoding="utf-8", errors="ignore") as fp:
                parts.append(fp.read())
        md = "\n\n---\n\n".join(parts)
        pages = len(re.findall(r"\f|<!-- page -->", md)) or len(mds)
        return {"markdown": md, "pages": pages, "files": [os.path.basename(m) for m in mds]}
    except subprocess.TimeoutExpired:
        return JSONResponse(status_code=504, content={"error": "parse timeout (>30min)"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

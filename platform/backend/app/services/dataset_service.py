"""数据集服务：路径、jsonl 读写、清洗脱敏、合成数据（QA/CoT）、Argilla 标注推送

注意（架构约定）：数据任务不新起容器 ——
- 解析：HTTP 调用常驻 MinerU 服务
- 清洗/合成：backend 进程内线程执行
- 版本控制：DVC subprocess
"""
import json
import os
import re
import time
import logging

import requests

from ..config import settings

log = logging.getLogger("dataset")

# ---------- 路径 ----------

def dataset_repo_dir(project_id: int, name: str) -> str:
    safe = re.sub(r"[^\w\-]", "_", name)
    return os.path.join(settings.data_dir, "datasets", f"p{project_id}", safe)


def version_dir(repo: str, version: int) -> str:
    return os.path.join(repo, f"v{version}")


def version_data_file(repo: str, version: int) -> str:
    return os.path.join(version_dir(repo, version), "data.jsonl")


def ensure_version_local(repo: str, version: int) -> str:
    """保证版本数据文件本地缓存可用（主存储在 rustfs，未命中自动下载），返回本地路径"""
    from . import storage
    p = version_data_file(repo, version)
    if not os.path.exists(p):
        storage.ensure_local(p)
    return p


def ensure_version_raw(repo: str, version: int) -> str:
    """保证版本原始文档（raw.md）本地缓存可用，返回本地路径"""
    from . import storage
    p = os.path.join(version_dir(repo, version), "raw.md")
    if not os.path.exists(p):
        storage.ensure_local(p)
    return p


# ---------- jsonl 读写 ----------

def read_jsonl(path: str) -> list[dict]:
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return rows


def write_jsonl(path: str, rows: list[dict]) -> int:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return len(rows)


def normalize(row: dict, fmt: str) -> dict | None:
    """统一为 alpaca 结构 {instruction, input, output} / dpo {prompt, chosen, rejected}"""
    if fmt == "dpo":
        prompt = (row.get("prompt") or row.get("instruction") or "").strip()
        chosen = (row.get("chosen") or row.get("output") or "").strip()
        rejected = (row.get("rejected") or "").strip()
        if not prompt or not chosen:
            return None
        return {"prompt": prompt, "chosen": chosen, "rejected": rejected}
    # alpaca / sharegpt
    if "conversations" in row:
        msgs = row["conversations"]
        ins, out = "", ""
        for m in msgs:
            role = m.get("from") or m.get("role")
            val = m.get("value") or m.get("content") or ""
            if role in ("human", "user"):
                ins = val
            elif role in ("gpt", "assistant"):
                out = val
        row = {"instruction": ins, "output": out}
    ins = (row.get("instruction") or row.get("instruction_zh") or "").strip()
    inp = (row.get("input") or row.get("input_zh") or "").strip()
    out = (row.get("output") or row.get("output_zh") or "").strip()
    if not ins or not out:
        return None
    return {"instruction": ins, "input": inp, "output": out}


# ---------- 清洗脱敏 ----------

PII_RULES = {
    "phone": (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[PHONE]"),
    "idcard": (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[IDCARD]"),
    "email": (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "[EMAIL]"),
    "bankcard": (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[BANKCARD]"),
}


def pii_clean(text: str, enabled: list[str]) -> str:
    for key in enabled:
        rule = PII_RULES.get(key)
        if rule:
            text = rule[0].sub(rule[1], text)
    return text


def clean_rows(rows: list[dict], params: dict) -> tuple[list[dict], dict]:
    enabled = params.get("pii", ["phone", "idcard", "email", "bankcard"])
    custom = params.get("custom_regex", [])  # [{"pattern": "...", "replace": "..."}]
    min_len = int(params.get("min_len", 0))
    dedup = bool(params.get("dedup", True))
    stats = {"total": len(rows), "dropped": 0, "masked": 0}
    out, seen = [], set()
    for row in rows:
        changed = False
        for k in ("instruction", "input", "output", "prompt", "chosen", "rejected"):
            if k in row and isinstance(row[k], str):
                before = row[k]
                row[k] = pii_clean(before, enabled)
                for c in custom:
                    try:
                        row[k] = re.sub(c.get("pattern", ""), c.get("replace", "[MASK]"), row[k])
                    except re.error:
                        continue
                if row[k] != before:
                    changed = True
        if changed:
            stats["masked"] += 1
        ins = row.get("instruction") or row.get("prompt") or ""
        outt = row.get("output") or ""
        if len(ins) < min_len or (min_len and not outt):
            stats["dropped"] += 1
            continue
        if dedup:
            key = json.dumps([ins, outt], ensure_ascii=False)
            if key in seen:
                stats["dropped"] += 1
                continue
            seen.add(key)
        out.append(row)
    stats["kept"] = len(out)
    return out, stats


# ---------- Markdown → QA 切块 ----------

def chunk_markdown(md: str, max_chars: int = 1500) -> list[str]:
    paras = re.split(r"\n\s*\n", md)
    chunks, cur = [], ""
    for p in paras:
        p = p.strip()
        if not p:
            continue
        if len(cur) + len(p) > max_chars and cur:
            chunks.append(cur)
            cur = p
        else:
            cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return [c for c in chunks if len(c) >= 50]


def markdown_to_alpaca(md: str) -> list[dict]:
    """解析结果转文档知识样例（instruction=请总结/回答问题, output=原文块）"""
    rows = []
    for c in chunk_markdown(md):
        rows.append({
            "instruction": "以下是文档内容，请基于它回答问题：\n" + c,
            "input": "",
            "output": c,
        })
    return rows


# ---------- 合成数据（QA / CoT） ----------

# few-shot 示例对格式合规至关重要：小模型（≤1B base）无示例时经常输出裸 JSON 对象或
# 直接回答片段内的问题（实测 0.5B base 无示例 0/10 可解析，加示例后 8/10）
_FEW_SHOT_QA = ("示例（片段：北京是中国的首都。）："
                "[{{\"instruction\": \"中国的首都是哪里？\", \"input\": \"\", \"output\": \"北京\"}}]\n")
_FEW_SHOT_COT = ("示例（片段：地球绕太阳公转。）："
                 "[{{\"instruction\": \"为什么地球上有昼夜交替？\", \"input\": \"\", "
                 "\"output\": \"思考过程：地球自转导致面向太阳一侧为白天、背向一侧为黑夜，自转一周即一次昼夜交替。\\n最终答案：地球自转。\"}}]\n")

QA_PROMPT = (
    "你是高质量大模型微调数据构造专家。基于下面【文档片段】生成 {n} 个高质量问答对，"
    "要求：问题具体、答案准确且完全基于片段、覆盖片段关键信息、语言为中文。\n"
    "只输出 JSON 数组，元素为 {{\"instruction\": \"问题\", \"input\": \"\", \"output\": \"答案\"}}，"
    "不要输出任何其他文字。\n" + _FEW_SHOT_QA +
    "\n【文档片段】\n{text}"
)

COT_PROMPT = (
    "你是高质量大模型推理数据构造专家。基于下面【文档片段】构造 {n} 个需要多步推理的问题，"
    "并给出完整思维链（CoT）解答。要求：推理步骤清晰、答案正确、语言为中文。\n"
    "只输出 JSON 数组，元素为 {{\"instruction\": \"问题\", \"input\": \"\", "
    "\"output\": \"思考过程：...\\n最终答案：...\"}}，不要输出任何其他文字。\n" + _FEW_SHOT_COT +
    "\n【文档片段】\n{text}"
)


def _extract_json_array(text: str) -> list:
    """从模型输出解析 JSON 数组 / 对象。

    小 base 模型格式遵循差：常见输出为多个裸 JSON 对象（无数组括号）夹杂原文。
    策略：先取整体 JSON 数组；失败/为空时退化为括号平衡扫描，收集所有完整 JSON 对象。
    """
    m = re.search(r"\[.*\]", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, list) and data:
                return data
        except json.JSONDecodeError:
            pass
    objs, depth, start, in_str, esc = [], 0, None, False, False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            objs.append(obj)
                    except json.JSONDecodeError:
                        pass
                    start = None
    return objs


# 每 chunk 最多尝试次数：小模型格式违规率高，0 条解析结果时重采样（temperature>0 无 seed，各次独立）
SYNTH_MAX_ATTEMPTS = 3


def synth_from_chunks(chunks: list[str], mode: str, n_per_chunk: int,
                      base_url: str, model: str, api_key: str = "",
                      progress_cb=None) -> tuple[list[dict], str]:
    """调用 OpenAI 兼容端点合成 QA/CoT 数据。

    返回 (rows, last_raw)：last_raw 为最近一次调用的原始输出，全部失败时供任务错误信息附样本。
    """
    from openai import OpenAI
    client = OpenAI(api_key=api_key or "EMPTY", base_url=base_url)
    tpl = QA_PROMPT if mode == "qa" else COT_PROMPT
    rows, last_raw = [], ""
    total = len(chunks)
    for i, c in enumerate(chunks):
        for attempt in range(1, SYNTH_MAX_ATTEMPTS + 1):
            try:
                r = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": tpl.format(n=n_per_chunk, text=c[:6000])}],
                    max_tokens=3000, temperature=0.3)
            except Exception as e:
                log.warning("synth chunk %s attempt %s call failed: %s", i, attempt, e)
                time.sleep(0.5)
                continue
            last_raw = r.choices[0].message.content or ""
            got = _extract_json_array(last_raw)
            if got:
                rows.extend(got)
                break
            log.warning("synth chunk %s attempt %s: 无可解析 JSON（原始输出头: %.120s）",
                        i, attempt, last_raw)
        if progress_cb:
            progress_cb(i + 1, total)
        time.sleep(0.2)
    return rows, last_raw


# ---------- Argilla 标注推送 ----------

def push_to_argilla(dataset_name: str, rows: list[dict], project_tag: str) -> str:
    """把样例推送到 Argilla 数据集，返回标注页 URL（argilla 2.x 客户端 API）"""
    ds_name = f"{project_tag}__{dataset_name}"
    # Web UI 经 nginx 挂在 /argilla/ 前缀下，默认工作区 argilla
    url = f"/argilla/argilla/datasets/{ds_name}"
    try:
        from argilla import Argilla, Dataset, TextField, LabelQuestion
        from argilla.settings._resource import Settings
        client = Argilla(api_url=settings.argilla_url, api_key=settings.argilla_api_key)
        ds = next((d for d in client.datasets.list() if d.name == ds_name), None)
        if ds is None:
            schema = Settings()
            schema.add(TextField(name="instruction", title="指令"))
            schema.add(TextField(name="input", title="输入"))
            schema.add(TextField(name="output", title="输出"))
            schema.add(LabelQuestion(name="quality", title="质量",
                                     labels=["accept", "reject", "needs_edit"]))
            ds = Dataset(name=ds_name, settings=schema, client=client)
            ds.create()
            log.info("argilla dataset created: %s", ds_name)
        records = [
            {"instruction": str(r.get("instruction", "")),
             "input": str(r.get("input", "")),
             "output": str(r.get("output", ""))}
            for r in rows[:2000]
        ]
        # 2.x 客户端：records 为可调用对象，records([...]) 即批量写入
        ds.records(records)
    except Exception as e:
        log.warning("argilla push failed (标注入口仍可访问): %s", e)
    return url


# ---------- MinerU 解析 ----------

def mineru_parse(file_path: str, file_name: str) -> dict:
    url = f"{settings.mineru_url}/parse"
    with open(file_path, "rb") as f:
        r = requests.post(url, files={"file": (file_name, f)}, timeout=1800)
    r.raise_for_status()
    return r.json()

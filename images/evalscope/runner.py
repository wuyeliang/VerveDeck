#!/usr/bin/env python3
"""VerveDeck 评测运行器（在 evalscope 容器内执行）
输入: 一个 JSON 任务文件 /workspace/task.json
  {
    "mode": "rule" | "judge" | "both" | "benchmark",
    "target_url": "http://ip:8000/v1",          # OpenAI 兼容端点
    "target_model": "served-model-name",
    "test_file": "/data/evals/<id>/test.jsonl", # {prompt, reference}
    "judge_url": "http://ip:8000/v1" | null,
    "judge_model": "..." ,
    "benchmark": "gsm8k" | null,   # benchmark 模式透传给 evalscope CLI
    "limit": 100,
    "out_dir": "/data/evals/<id>"
  }
输出: <out_dir>/result.json  {summary: {...}, details: [...]}
"""
import json, os, re, sys, subprocess, urllib.request
from collections import Counter

def call_chat(base_url, model, messages, max_tokens=1024, temperature=0.0):
    import openai
    client = openai.OpenAI(api_key="EMPTY", base_url=base_url)
    r = client.chat.completions.create(model=model, messages=messages,
                                       max_tokens=max_tokens, temperature=temperature)
    return r.choices[0].message.content or ""

def norm(s):
    return re.sub(r"\s+", "", (s or "")).lower()

def token_f1(pred, ref):
    p, r = set(norm(pred).split()) if False else list(norm(pred)), list(norm(ref))
    if not p or not r:
        return 0.0
    common = Counter(p) & Counter(r)
    n = sum(common.values())
    if n == 0:
        return 0.0
    return 2 * n / (len(p) + len(r))

def rouge_l(pred, ref):
    try:
        from rouge_chinese import Rouge
        rouge = Rouge()
        score = rouge.get_scores(" ".join(norm(pred)), " ".join(norm(ref)))[0]
        return score["rouge-l"]["f"]
    except Exception:
        return token_f1(pred, ref)

def rule_score(pred, ref):
    pn, rn = norm(pred), norm(ref)
    return {
        "exact": 1.0 if pn == rn else 0.0,
        "contains": 1.0 if rn and rn in pn else 0.0,
        "f1": token_f1(pred, ref),
        "rouge_l": rouge_l(pred, ref),
    }

JUDGE_PROMPT = (
    "你是一名严格的评测专家。请根据【参考答案】对【模型回答】打分（1-5 分整数）。\n"
    "评分标准：5=完全正确且完整；4=基本正确，个别细节缺失；3=部分正确；2=大部分错误；1=完全错误。\n"
    "只输出 JSON：{\"score\": <1-5>, \"reason\": \"<20字内理由>\"}\n\n"
    "【问题】\n{prompt}\n\n【参考答案】\n{reference}\n\n【模型回答】\n{pred}"
)

def judge_score(judge_url, judge_model, prompt, ref, pred):
    try:
        out = call_chat(judge_url, judge_model,
                        [{"role": "user", "content": JUDGE_PROMPT.format(prompt=prompt, reference=ref, pred=pred)}],
                        max_tokens=256)
        m = re.search(r"\{.*?\}", out, re.S)
        obj = json.loads(m.group(0)) if m else {}
        return {"score": float(obj.get("score", 1)), "reason": obj.get("reason", "")}
    except Exception as e:
        return {"score": 0.0, "reason": f"judge error: {e}"}

def run_benchmark(task):
    import os
    env = dict(os.environ)
    cmd = ["evalscope", "eval",
           "--model", task["target_model"],
           "--eval-type", "openai",
           "--api-url", task["target_url"],
           "--datasets", task["benchmark"]]
    if task.get("limit"):
        cmd += ["--limit", str(task["limit"])]
    out_dir = task["out_dir"]
    cmd += ["--workspace", out_dir]
    print("RUN:", " ".join(cmd), flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True, env=env)
    print(p.stdout[-4000:], flush=True)
    if p.stderr:
        print(p.stderr[-4000:], flush=True)
    # 收集 evalscope 输出下的 summary
    summary = None
    for root, _, files in os.walk(out_dir):
        for f in files:
            if f.endswith(".json") and "summary" in f:
                try:
                    with open(os.path.join(root, f)) as fp:
                        summary = json.load(fp)
                except Exception:
                    pass
    result = {"mode": "benchmark", "benchmark": task["benchmark"],
              "exit_code": p.returncode, "summary": summary}
    with open(os.path.join(out_dir, "result.json"), "w") as fp:
        json.dump(result, fp, ensure_ascii=False, indent=1)
    return result

def main():
    with open("/workspace/task.json") as f:
        task = json.load(f)
    out_dir = task["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    if task.get("mode") == "benchmark":
        run_benchmark(task)
        return

    samples = []
    with open(task["test_file"]) as f:
        for line in f:
            line = line.strip()
            if line:
                samples.append(json.loads(line))
    if task.get("limit"):
        samples = samples[: int(task["limit"])]

    details, scores = [], []
    for i, s in enumerate(samples):
        prompt = s.get("prompt") or ""
        ref = s.get("reference") or ""
        row = {"index": i, "prompt": prompt[:200], "reference": ref[:200]}
        try:
            pred = call_chat(task["target_url"], task["target_model"],
                             [{"role": "user", "content": prompt}],
                             max_tokens=int(task.get("max_tokens", 1024)))
        except Exception as e:
            row["error"] = str(e)
            row["pred"] = ""
            details.append(row)
            continue
        row["pred"] = pred[:1000]
        if task["mode"] in ("rule", "both"):
            row["rule"] = rule_score(pred, ref)
            scores.append((row["rule"]["f1"] + row["rule"]["rouge_l"]) / 2)
        if task["mode"] in ("judge", "both"):
            row["judge"] = judge_score(task["judge_url"], task["judge_model"], prompt, ref, pred)
            scores.append(row["judge"]["score"] / 5.0)
        details.append(row)
        if (i + 1) % 20 == 0:
            print(f"progress {i+1}/{len(samples)}", flush=True)

    summary = {
        "mode": task["mode"],
        "total": len(samples),
        "avg_score": round(sum(scores) / len(scores), 4) if scores else 0.0,
        "pass_rate": round(sum(1 for x in scores if x >= 0.6) / len(scores), 4) if scores else 0.0,
    }
    if task["mode"] in ("rule", "both"):
        summary["avg_exact"] = round(sum(d.get("rule", {}).get("exact", 0) for d in details) / max(len(details), 1), 4)
        summary["avg_contains"] = round(sum(d.get("rule", {}).get("contains", 0) for d in details) / max(len(details), 1), 4)
        summary["avg_f1"] = round(sum(d.get("rule", {}).get("f1", 0) for d in details) / max(len(details), 1), 4)
    if task["mode"] in ("judge", "both"):
        js = [d.get("judge", {}).get("score", 0) for d in details]
        summary["judge_avg"] = round(sum(js) / max(len(js), 1), 3)
        summary["judge_dist"] = dict(Counter(int(x) for x in js))
    with open(os.path.join(out_dir, "result.json"), "w") as fp:
        json.dump({"summary": summary, "details": details}, fp, ensure_ascii=False, indent=1)
    print("DONE", json.dumps(summary, ensure_ascii=False), flush=True)

if __name__ == "__main__":
    main()

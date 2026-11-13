# -*- coding: utf-8 -*-
"""数据集服务纯函数单测（normalize / PII 脱敏 / 清洗去重 / 切块 / JSON 抽取 / jsonl 读写）"""
import json
import os

import pytest

from app.services import dataset_service as ds


# ---------------- normalize ----------------

@pytest.mark.parametrize("row,fmt,expect", [
    ({"instruction": "q", "input": "i", "output": "a"}, "alpaca",
     {"instruction": "q", "input": "i", "output": "a"}),
    ({"instruction_zh": "问", "output_zh": "答"}, "alpaca",
     {"instruction": "问", "input": "", "output": "答"}),
    ({"instruction": "q"}, "alpaca", None),                      # 缺 output
    ({"output": "a"}, "alpaca", None),                           # 缺 instruction
    ({"conversations": [{"from": "human", "value": "你好"},
                        {"from": "gpt", "value": "您好"}]}, "alpaca",
     {"instruction": "你好", "input": "", "output": "您好"}),
    ({"conversations": [{"role": "user", "content": "u"},
                        {"role": "assistant", "content": "m"}]}, "alpaca",
     {"instruction": "u", "input": "", "output": "m"}),
    ({"prompt": "p", "chosen": "c", "rejected": "r"}, "dpo",
     {"prompt": "p", "chosen": "c", "rejected": "r"}),
    ({"instruction": "p", "output": "c"}, "dpo",                 # 兼容 alpaca 字段
     {"prompt": "p", "chosen": "c", "rejected": ""}),
    ({"chosen": "c"}, "dpo", None),                              # 缺 prompt
    ({"prompt": "p"}, "dpo", None),                              # 缺 chosen
    ({"prompt": "", "chosen": "c"}, "dpo", None),                # 空 prompt
    ({"instruction": "q", "output": "a"}, "sharegpt",
     {"instruction": "q", "input": "", "output": "a"}),
    ({}, "alpaca", None),
    ({"instruction": "  ", "output": "a"}, "alpaca", None),      # 空白 instruction
])
def test_normalize(row, fmt, expect):
    assert ds.normalize(row, fmt) == expect


# ---------------- PII 脱敏 ----------------

@pytest.mark.parametrize("text,enabled,expect", [
    ("手机 13812345678", ["phone"], "手机 [PHONE]"),
    ("13812345678 和 19998887777", ["phone"], "[PHONE] 和 [PHONE]"),
    ("213812345678", ["phone"], "213812345678"),                # 11 位前还有数字，不匹配
    ("138123456789", ["phone"], "138123456789"),                # 后跟数字，不匹配
    ("tel:12345678901", ["phone"], "tel:12345678901"),          # 12 开头非手机号段
    ("邮箱 a.b+c@test-domain.com", ["email"], "邮箱 [EMAIL]"),
    ("no email here", ["email"], "no email here"),
    ("身份证 110101199003077758", ["idcard"], "身份证 [IDCARD]"),
    ("身份证 11010119900307775X", ["idcard"], "身份证 [IDCARD]"),  # 末位 X
    ("短号 12345678901234567890123", ["idcard"], "短号 12345678901234567890123"),  # 19 位数字→bankcard 才匹配，idcard 不匹配
    ("卡号 6222020200112233445", ["bankcard"], "卡号 [BANKCARD]"),
    ("卡号 622202", ["bankcard"], "卡号 622202"),               # 16 位以下不匹配
    ("混合 13812345678 / a@b.cn / 6222020200112233445",
     ["phone", "email", "bankcard"],
     "混合 [PHONE] / [EMAIL] / [BANKCARD]"),
    ("什么都不含", ["phone", "email", "idcard", "bankcard"], "什么都不含"),
    ("手机 13812345678", [], "手机 13812345678"),               # 未启用任何规则
    ("手机 13812345678", ["not_exist_rule"], "手机 13812345678"),  # 未知规则忽略
])
def test_pii_clean(text, enabled, expect):
    assert ds.pii_clean(text, enabled) == expect


# ---------------- clean_rows ----------------

def _rows():
    return [
        {"instruction": "重复题", "input": "", "output": "答"},
        {"instruction": "重复题", "input": "", "output": "答"},
        {"instruction": "手机 13812345678", "input": "", "output": "ok"},
        {"instruction": "短", "input": "", "output": "x"},
    ]


def test_clean_dedup_drops_duplicate():
    out, stats = ds.clean_rows(_rows(), {"dedup": True, "pii": [], "min_len": 0})
    assert stats["total"] == 4 and stats["dropped"] == 1 and stats["kept"] == 3
    assert len(out) == 3


def test_clean_dedup_disabled_keeps_all():
    out, stats = ds.clean_rows(_rows(), {"dedup": False, "pii": [], "min_len": 0})
    assert stats["kept"] == 4


def test_clean_pii_masks_and_counts():
    out, stats = ds.clean_rows(_rows(), {"dedup": True, "pii": ["phone"], "min_len": 0})
    assert stats["masked"] == 1
    assert any("[PHONE]" in r["instruction"] for r in out)


def test_clean_min_len_drops_short():
    out, stats = ds.clean_rows(_rows(), {"dedup": False, "pii": [], "min_len": 2})
    assert any("短" in r["instruction"] for r in out) is False
    assert stats["dropped"] == 1


def test_clean_custom_regex():
    rows = [{"instruction": "代号 ALPHA 泄露", "input": "", "output": "x"}]
    out, stats = ds.clean_rows(rows, {
        "dedup": False, "pii": [], "min_len": 0,
        "custom_regex": [{"pattern": r"ALPHA", "replace": "[SECRET]"}]})
    assert out[0]["instruction"] == "代号 [SECRET] 泄露"
    assert stats["masked"] == 1


def test_clean_bad_custom_regex_skipped():
    rows = [{"instruction": "ALPHA", "input": "", "output": "x"}]
    out, stats = ds.clean_rows(rows, {
        "dedup": False, "pii": [], "min_len": 0,
        "custom_regex": [{"pattern": "([bad", "replace": "X"}]})
    assert out[0]["instruction"] == "ALPHA"          # 非法正则跳过，不崩


def test_clean_dpo_rows_masked():
    rows = [{"prompt": "联系 a@b.cn", "chosen": "好", "rejected": "不好"}]
    out, stats = ds.clean_rows(rows, {"dedup": True, "pii": ["email"], "min_len": 0})
    assert out[0]["prompt"] == "联系 [EMAIL]"


def test_clean_stats_consistent():
    out, stats = ds.clean_rows(_rows(), {})
    assert stats["total"] == stats["dropped"] + stats["kept"]


# ---------------- chunk_markdown / markdown_to_alpaca ----------------

def test_chunk_splits_long_doc():
    # 每段 602 字符：合并两段 1204 ≤ 1500，第三段超限 → 3 个 chunk
    md = "\n\n".join(f"段落{i}" + "内容" * 300 for i in range(6))
    chunks = ds.chunk_markdown(md, max_chars=1500)
    assert len(chunks) >= 2
    assert all(len(c) <= 1500 + 50 for c in chunks)


def test_chunk_drops_short_paras():
    assert ds.chunk_markdown("短文本\n\n另一段也不够长" ) == []


def test_chunk_empty_input():
    assert ds.chunk_markdown("") == []
    assert ds.chunk_markdown("\n\n  \n") == []


def test_chunk_single_small_para():
    md = "一段" + "字" * 60
    chunks = ds.chunk_markdown(md)
    assert len(chunks) == 1 and len(chunks[0]) >= 50


def test_markdown_to_alpaca_shape():
    # 每块 803 字符：两块合并 1606 > 1500 → 每块独立成 chunk，共 3 行
    md = "\n\n".join("知识块" + "X" * 800 for _ in range(3))
    rows = ds.markdown_to_alpaca(md)
    assert len(rows) == 3
    assert all(r["instruction"].startswith("以下是文档内容") for r in rows)
    assert all(r["output"] for r in rows)


# ---------------- _extract_json_array ----------------

@pytest.mark.parametrize("text,expect_n", [
    ('[{"a": 1}, {"a": 2}]', 2),
    ('前缀说明 [{"a": 1}] 后缀', 1),
    ('[]', 0),
    ('没有数组', 0),
    ('[broken json', 0),
    ('{"instruction": "q", "output": "a"}', 1),  # 单对象兜底（base 模型常见输出）
    ('{"broken": json', 0),
    ('[1, 2, 3]', 3),
])
def test_extract_json_array(text, expect_n):
    assert len(ds._extract_json_array(text)) == expect_n


# ---------------- jsonl 读写 / 路径 ----------------

def test_jsonl_roundtrip(tmp_path):
    p = str(tmp_path / "sub" / "data.jsonl")
    rows = [{"中文": "值", "n": 1}, {"a": None}]
    assert ds.write_jsonl(p, rows) == 2
    assert ds.read_jsonl(p) == rows


def test_jsonl_skips_bad_lines(tmp_path):
    p = str(tmp_path / "x.jsonl")
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"ok": 1}\nnot-json\n\n{"ok": 2}\n')
    assert ds.read_jsonl(p) == [{"ok": 1}, {"ok": 2}]


def test_repo_dir_sanitizes_name():
    d = ds.dataset_repo_dir(7, "my ds/with:chars")
    assert d.endswith(os.path.join("p7", "my_ds_with_chars"))


def test_version_paths():
    repo = "/x/repo"
    assert ds.version_dir(repo, 3) == "/x/repo/v3"
    assert ds.version_data_file(repo, 3) == "/x/repo/v3/data.jsonl"

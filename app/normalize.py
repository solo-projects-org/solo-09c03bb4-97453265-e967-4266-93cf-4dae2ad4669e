"""英文词归一化（错词→正词替换）。

规则（对应需求）：
- 按完整英文词匹配，忽略大小写；非字母字符原样保留。
- 映射键统一小写；命中后替换为词典中给出的正词（保留正词原始写法）。
- 每个词 token 只查表一次，因此天然不存在链式替换，
  正词不会再被当作错词处理（上传时另有集合互斥校验）。
"""
from __future__ import annotations

import re
from typing import Mapping

#: 完整英文词：连续 ASCII 字母串。词边界由非字母字符自然界定。
WORD_RE = re.compile(r"[A-Za-z]+")
#: 词典条目合法形态。
WORD_HEAD_RE = re.compile(r"^[A-Za-z]+$")


def build_mapping(entries) -> dict[str, str]:
    """把 (wrong, correct) 序列编译为小写键映射。"""
    mapping: dict[str, str] = {}
    for wrong, correct in entries:
        mapping[wrong.lower()] = correct
    return mapping


def normalize_text(text: str | None, mapping: Mapping[str, str]) -> str:
    """按词典对文本做整词、忽略大小写替换。"""
    if not text:
        return ""
    if not mapping:
        return text

    def _repl(m: re.Match) -> str:
        return mapping.get(m.group(0).lower(), m.group(0))

    return WORD_RE.sub(_repl, text)


def normalize_query(query: str, mapping: Mapping[str, str]) -> list[str]:
    """查询词走与题录完全相同的归一规则，返回归一后的词列表。"""
    return [mapping.get(t.lower(), t) for t in WORD_RE.findall(query or "")]

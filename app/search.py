"""FTS5 检索：所有查询词归一后须全部命中（AND 语义）。"""
from __future__ import annotations

import sqlite3

from . import state
from .db import conn, meta_value
from .normalize import normalize_query

# FTS5 字符串字面量：双引号转义，杜绝注入。
def _fts_quote(token: str) -> str:
    return '"' + token.replace('"', '""') + '"'


def search(query: str, limit: int = 100) -> dict:
    c = conn()
    mapping = state.current_mapping()
    terms = normalize_query(query, mapping)
    if not terms:
        return {"query": query, "normalized_terms": [], "results": []}

    fts = meta_value(c, "active_fts")
    dict_id = int(meta_value(c, "active_dict"))
    match_expr = " AND ".join(_fts_quote(t) for t in terms)

    # 单条只读语句天然是一个快照：词典(active_dict)、命中(该 FTS 表内容)、
    # 题录(records 现行行)同处一个已提交状态。
    sql = f"""
        SELECT r.doi, r.title, r.abstract, r.revision
        FROM {fts} f
        JOIN records r ON r.id = f.rowid
        WHERE {fts} MATCH ? AND r.superseded = 0
        ORDER BY rank
        LIMIT ?
    """
    rows = c.execute(sql, (match_expr, limit)).fetchall()
    return {
        "query": query,
        "normalized_terms": terms,
        "dict_version_id": dict_id,
        "fts_table": fts,
        "count": len(rows),
        "results": [
            {"doi": r["doi"], "title": r["title"],
             "abstract": r["abstract"], "revision": r["revision"]}
            for r in rows
        ],
    }

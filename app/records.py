"""题录 CSV 导入。

批次规则（整批事务，全收或全退）：
- 修订号高于现行 -> 更正采纳，旧文标记 superseded；
- 同修订号且同题录内容（题名+摘要哈希一致）-> 复用，不重复写；
- 同修订号但内容不同 -> 整批拒收；
- 修订号低于现行 -> 整批拒收；
- 批内同一 DOI 重复 -> 整批拒收。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3

from . import state
from .db import conn, meta_value, write_lock
from .normalize import normalize_text

REQUIRED_COLUMNS = ("doi", "revision", "title")
OPTIONAL_COLUMNS = ("abstract",)


class ImportError_(Exception):
    """整批拒收级别的错误（400）。"""


def _parse_csv(content: bytes) -> tuple[list[str], list[dict[str, str]]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ImportError_(f"CSV 必须为 UTF-8 编码：{exc}") from exc
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise ImportError_("CSV 缺少表头")
    headers = [h.strip().lower() for h in reader.fieldnames]
    missing = [c for c in REQUIRED_COLUMNS if c not in headers]
    if missing:
        cn = {"doi": "DOI", "revision": "修订号", "title": "题名"}
        raise ImportError_(
            "CSV 缺少必需列：" + ", ".join(f"{c}（{cn[c]}）" for c in missing)
        )
    rows: list[dict[str, str]] = []
    for raw in reader:
        rows.append({(k or "").strip().lower(): (v or "").strip()
                     for k, v in raw.items()})
    return headers, rows


def import_records_csv(content: bytes, filename: str | None) -> dict:
    headers, rows = _parse_csv(content)
    if not rows:
        raise ImportError_("CSV 没有数据行")

    c = conn()
    mapping = state.current_mapping()
    planned: list[dict] = []
    seen_dois: set[str] = set()
    accepted = reused = 0
    reasons: list[str] = []

    # 阶段一：逐行判定（不写库）。
    for idx, row in enumerate(rows, start=1):
        doi = row.get("doi", "").strip()
        rev_raw = row.get("revision", "").strip()
        title = row.get("title", "").strip()
        abstract = row.get("abstract", "").strip()
        if not doi:
            raise ImportError_(f"第 {idx} 行：DOI 为空")
        if not title:
            raise ImportError_(f"第 {idx} 行：题名为空")
        if not rev_raw:
            raise ImportError_(f"第 {idx} 行：修订号为空")
        try:
            revision = int(rev_raw)
            if revision < 0 or str(revision) != rev_raw:
                raise ValueError
        except ValueError as exc:
            raise ImportError_(
                f"第 {idx} 行：修订号必须是非负整数，得到 {rev_raw!r}"
            ) from exc
        if doi in seen_dois:
            raise ImportError_(f"第 {idx} 行：批次内 DOI 重复：{doi}")
        seen_dois.add(doi)

        digest = hashlib.sha256(
            f"{title}\x1f{abstract}".encode("utf-8")
        ).hexdigest()
        live = c.execute(
            "SELECT id, revision, content_hash FROM records "
            "WHERE doi=? AND superseded=0",
            (doi,),
        ).fetchone()

        if live is None:
            planned.append({"doi": doi, "revision": revision, "title": title,
                            "abstract": abstract, "hash": digest,
                            "action": "accept"})
            accepted += 1
            continue

        if revision > live["revision"]:
            planned.append({"doi": doi, "revision": revision, "title": title,
                            "abstract": abstract, "hash": digest,
                            "action": "accept", "old_id": live["id"]})
            accepted += 1
        elif revision == live["revision"] and digest == live["content_hash"]:
            planned.append({"doi": doi, "action": "reuse"})
            reused += 1
        elif revision == live["revision"]:
            raise ImportError_(
                f"第 {idx} 行：DOI {doi} 同版异文（修订号 {revision} "
                "已存在但题名/摘要不同），整批拒收"
            )
        else:
            raise ImportError_(
                f"第 {idx} 行：DOI {doi} 修订号 {revision} 低于现行 "
                f"{live['revision']}，整批拒收"
            )

    # 阶段二：单事务提交（重建期间照常写入【当前】FTS，立即可检索）。
    active_fts = state.active_fts_name()
    active_dict = int(meta_value(c, "active_dict"))
    with write_lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            for item in planned:
                if item["action"] == "reuse":
                    continue
                # 先让旧行退出唯一现行索引，再插入新行（同一事务内）。
                if "old_id" in item:
                    c.execute(
                        "UPDATE records SET superseded=1 WHERE id=?",
                        (item["old_id"],),
                    )
                    c.execute(
                        f"DELETE FROM {active_fts} WHERE rowid=?",
                        (item["old_id"],),
                    )
                cur = c.execute(
                    "INSERT INTO records(doi, revision, title, abstract, "
                    "content_hash, dict_version_id) VALUES(?,?,?,?,?,?)",
                    (item["doi"], item["revision"], item["title"],
                     item["abstract"], item["hash"], active_dict),
                )
                rid = cur.lastrowid
                # 先删后插：FTS 中已存在的 rowid 直接 INSERT 会被当作更新，
                # 显式 DELETE 保证语义确定（正常路径下该 rowid 不存在）。
                c.execute(
                    f"DELETE FROM {active_fts} WHERE rowid=?", (rid,))
                c.execute(
                    f"INSERT INTO {active_fts}(rowid, title_norm, "
                    "abstract_norm) VALUES(?,?,?)",
                    (rid, normalize_text(item["title"], mapping),
                     normalize_text(item["abstract"], mapping)),
                )
            batch_id = c.execute(
                "INSERT INTO import_batches(filename, status, total, "
                "accepted, reused, detail) VALUES(?,?,?,?,?,?)",
                (filename, "accepted" if accepted else "reused",
                 len(rows), accepted, reused,
                 json.dumps(reasons or [p["action"] for p in planned],
                            ensure_ascii=False)),
            ).lastrowid
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    return {"batch_id": batch_id, "total": len(rows),
            "accepted": accepted, "reused": reused,
            "status": "accepted" if accepted else "reused"}

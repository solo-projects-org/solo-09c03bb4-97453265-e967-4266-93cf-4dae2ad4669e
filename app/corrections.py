"""人工校订：三方合并、冲突暂停、冲突裁决与供数 CSV。

术语
----
- B（基底）：保存人工校订时所见的 OCR 现行题名/摘要（``human_corrections.base_record_id``
  指向的 records 行；records 只取代不删除，基底永远可回溯）。
- O（现行 OCR）：当前未被取代的 records 行内容；新 OCR 修订入库时可能变化。
- H（人工版）：``human_corrections`` 现行行（每个 DOI 至多一条未取代行）。

逐字段三方合并规则（题名、摘要分别独立判定）：

| O 相对 B | H 相对 B | 处理 |
|---|---|---|
| 未改 | 未改 | 取 B（source=base） |
| 改 | 未改 | 自动取 O（source=ocr） |
| 未改 | 改 | 自动取 H（source=human） |
| 改 | 改，且 O==H | 直接采用同值（source=ocr） |
| 改 | 改，且 O!=H | **字段冲突**：整条 DOI 暂停供数，保留三方内容 |

任一字段冲突则整条记录 status=conflict，不进入供数 CSV；人工裁决只接受
``ocr / human / base`` 三方既有内容，且裁决绑定当时的 OCR 行与人工版 ID，
任一发生变化（新 OCR 修订、重新保存人工版都会触发重新合并）后旧页面提交得到 409，
旧裁决不能恢复供数。
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import sqlite3

from .db import conn, write_lock

#: 合并后字段值来源（manual 表示冲突后人工裁决）。
SOURCE_BASE = "base"
SOURCE_OCR = "ocr"
SOURCE_HUMAN = "human"
SOURCE_MANUAL = "manual"
VALID_RESOLUTION_SOURCES = (SOURCE_OCR, SOURCE_HUMAN, SOURCE_BASE)
FIELDS = ("title", "abstract")


class CorrectionError(Exception):
    """请求内容非法（400）。"""


class CorrectionConflict(Exception):
    """裁决页面已过期：所据 OCR 修订或人工版发生变化（409）。"""


def _hash(title: str, abstract: str) -> str:
    return hashlib.sha256(f"{title}\x1f{abstract}".encode("utf-8")).hexdigest()


def _merge_field(base: str, ocr: str, human: str) -> tuple[str, str, bool]:
    """返回 (合并值, 来源, 是否冲突)。冲突时合并值为 OCR 原值且不直接采用。"""
    ocr_changed = ocr != base
    human_changed = human != base
    if not ocr_changed and not human_changed:
        return base, SOURCE_BASE, False
    if ocr_changed and not human_changed:
        return ocr, SOURCE_OCR, False
    if human_changed and not ocr_changed:
        return human, SOURCE_HUMAN, False
    # 双方都改了
    if ocr == human:
        return ocr, SOURCE_OCR, False
    return ocr, "", True


def _reprocess(c: sqlite3.Connection, doi: str, live_id: int,
               base_id: int, human_id: int) -> dict:
    """对单个 DOI 重新执行三方合并并 upsert correction_states。

    调用方必须已持有写事务（导入批次内调用时与批次接纳同生共死）。
    """
    live = c.execute(
        "SELECT id, title, abstract FROM records WHERE id=? AND superseded=0",
        (live_id,),
    ).fetchone()
    base = c.execute(
        "SELECT id, title, abstract FROM records WHERE id=?", (base_id,)
    ).fetchone()
    human = c.execute(
        "SELECT id, title, abstract FROM human_corrections WHERE id=?",
        (human_id,),
    ).fetchone()
    if live is None or base is None or human is None:
        raise CorrectionError(f"DOI {doi}：合并所需的 OCR/人工版本缺失")

    merged: dict[str, str] = {}
    sources: dict[str, str] = {}
    conflict_flags: dict[str, bool] = {}
    conflict_detail: dict[str, dict] = {}
    for field in FIELDS:
        value, source, is_conflict = _merge_field(
            base[field], live[field], human[field]
        )
        conflict_flags[field] = is_conflict
        if is_conflict:
            # 暂停供数：合并值留空，三方内容完整保留供人工选择。
            merged[field] = ""
            sources[field] = ""
            conflict_detail[field] = {
                "base": base[field], "ocr": live[field], "human": human[field],
            }
        else:
            merged[field] = value
            sources[field] = source

    has_conflict = any(conflict_flags.values())
    status = "conflict" if has_conflict else "clean"
    c.execute(
        """
        INSERT INTO correction_states(
            doi, status, live_record_id, base_record_id, human_correction_id,
            merged_title, merged_abstract, title_source, abstract_source,
            title_conflict, abstract_conflict, conflict_detail,
            resolution_ocr_record_id, resolution_human_id, resolution_detail,
            updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
        ON CONFLICT(doi) DO UPDATE SET
            status=excluded.status,
            live_record_id=excluded.live_record_id,
            base_record_id=excluded.base_record_id,
            human_correction_id=excluded.human_correction_id,
            merged_title=excluded.merged_title,
            merged_abstract=excluded.merged_abstract,
            title_source=excluded.title_source,
            abstract_source=excluded.abstract_source,
            title_conflict=excluded.title_conflict,
            abstract_conflict=excluded.abstract_conflict,
            conflict_detail=excluded.conflict_detail,
            -- 重新处理一律作废旧裁决；旧页面无法再恢复供数。
            resolution_ocr_record_id=NULL,
            resolution_human_id=NULL,
            resolution_detail=NULL,
            updated_at=datetime('now')
        """,
        (
            doi, status, live_id, base_id, human_id,
            merged["title"], merged["abstract"],
            sources["title"], sources["abstract"],
            int(conflict_flags["title"]), int(conflict_flags["abstract"]),
            json.dumps(conflict_detail, ensure_ascii=False)
            if conflict_detail else None,
            None, None, None,
        ),
    )
    return {"doi": doi, "status": status,
            "conflict_fields": [f for f in FIELDS if conflict_flags[f]]}


def reprocess_after_import(c: sqlite3.Connection, doi: str,
                           new_record_id: int) -> dict | None:
    """OCR 导入事务内回调：新修订入库后重新合并受影响的人工校订。

    无在校人工版的 DOI 无 correction_states 行，直接跳过（供数沿用最新 OCR）。
    """
    state = c.execute(
        "SELECT * FROM correction_states WHERE doi=?", (doi,)
    ).fetchone()
    if state is None:
        return None
    human = c.execute(
        "SELECT id FROM human_corrections WHERE doi=? AND superseded=0",
        (doi,),
    ).fetchone()
    if human is None:  # 理论上不会发生：状态行必然由人工版创建
        return None
    return _reprocess(c, doi, new_record_id, state["base_record_id"],
                      human["id"])


def _log_batch(kind: str, status: str, total: int, detail: str) -> int:
    c = conn()
    with write_lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            bid = c.execute(
                "INSERT INTO correction_batches(kind, status, total, detail) "
                "VALUES(?,?,?,?)",
                (kind, status, total, detail),
            ).lastrowid
            c.execute("COMMIT")
            return bid
        except Exception:
            c.execute("ROLLBACK")
            raise


def submit_corrections(items: list[dict]) -> dict:
    """提交一批人工校订（整批事务，全收或全退）。

    每项：{"doi": str, "title": str, "abstract": str（可空）}。
    以提交时所见 OCR 现行行为基底保存人工版，并立即重新合并：
    可自动合并的当场更新供数；冲突的整条暂停。
    """
    if not items:
        raise CorrectionError("校订批次为空")
    cleaned: list[dict] = []
    seen: set[str] = set()
    for idx, item in enumerate(items, start=1):
        doi = (item.get("doi") or "").strip()
        title = (item.get("title") or "").strip()
        abstract = (item.get("abstract") or "").strip()
        if not doi:
            raise CorrectionError(f"第 {idx} 项：DOI 为空")
        if not title:
            raise CorrectionError(f"第 {idx} 项：题名不能为空（DOI {doi}）")
        if doi in seen:
            raise CorrectionError(f"第 {idx} 项：批次内 DOI 重复：{doi}")
        seen.add(doi)
        cleaned.append({"doi": doi, "title": title, "abstract": abstract})

    c = conn()
    reprocessed: list[dict] = []
    with write_lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            for item in cleaned:
                live = c.execute(
                    "SELECT id, revision FROM records "
                    "WHERE doi=? AND superseded=0",
                    (item["doi"],),
                ).fetchone()
                if live is None:
                    raise CorrectionError(
                        f"DOI {item['doi']}：尚无现行 OCR 题录，无法校订"
                    )
                # 旧人工版整体取代（保留行供审计），新人工版以当前 OCR 为基底。
                c.execute(
                    "UPDATE human_corrections SET superseded=1 "
                    "WHERE doi=? AND superseded=0",
                    (item["doi"],),
                )
                hc_id = c.execute(
                    "INSERT INTO human_corrections(doi, base_record_id, "
                    "base_revision, title, abstract, content_hash) "
                    "VALUES(?,?,?,?,?,?)",
                    (item["doi"], live["id"], live["revision"],
                     item["title"], item["abstract"],
                     _hash(item["title"], item["abstract"])),
                ).lastrowid
                reprocessed.append(
                    _reprocess(c, item["doi"], live["id"], live["id"], hc_id)
                )
            batch_id = c.execute(
                "INSERT INTO correction_batches(kind, status, total, detail) "
                "VALUES('submit','committed',?,?)",
                (len(cleaned),
                 json.dumps(reprocessed, ensure_ascii=False)),
            ).lastrowid
            c.execute("COMMIT")
        except Exception as exc:
            c.execute("ROLLBACK")
            # 拒收留痕，独立事务：不改变任何校订/状态数据。
            try:
                _log_batch("submit", "rejected", len(cleaned),
                           json.dumps([str(exc)], ensure_ascii=False))
            except Exception:
                pass
            raise

    conflicts = [r for r in reprocessed if r["status"] == "conflict"]
    return {"batch_id": batch_id, "total": len(cleaned),
            "auto_merged": len(reprocessed) - len(conflicts),
            "conflicts": len(conflicts),
            "status": "committed", "results": reprocessed}


def resolve_conflict(doi: str, resolutions: dict,
                     expected_ocr_record_id: int,
                     expected_human_correction_id: int) -> dict:
    """人工裁决冲突字段。

    resolutions: {"title": {"source": "ocr|human|base"}, "abstract": {...}}，
    只需给出当前处于冲突的字段。expected_* 为页面所见版本绑定，任一失配返回 409。
    """
    doi = (doi or "").strip()
    if not doi:
        raise CorrectionError("DOI 为空")
    if not isinstance(resolutions, dict) or not resolutions:
        raise CorrectionError("resolutions 至少包含一个冲突字段的裁决")

    c = conn()
    with write_lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            state = c.execute(
                "SELECT * FROM correction_states WHERE doi=?", (doi,)
            ).fetchone()
            if state is None:
                raise CorrectionError(f"DOI {doi}：不存在校订状态")
            if state["status"] != "conflict":
                raise CorrectionError(
                    f"DOI {doi}：当前无冲突（状态 {state['status']}），无需裁决"
                )
            # 版本绑定守卫：所据 OCR 修订或人工版任一变化，旧页面一律拒绝。
            if (state["live_record_id"] != expected_ocr_record_id
                    or state["human_correction_id"]
                    != expected_human_correction_id):
                raise CorrectionConflict(
                    f"DOI {doi}：页面所据 OCR 行 {expected_ocr_record_id}/"
                    f"人工版 {expected_human_correction_id} 已变化"
                    f"（现行 {state['live_record_id']}/"
                    f"{state['human_correction_id']}），请刷新后重新裁决"
                )

            live = c.execute(
                "SELECT title, abstract FROM records WHERE id=?",
                (state["live_record_id"],),
            ).fetchone()
            base = c.execute(
                "SELECT title, abstract FROM records WHERE id=?",
                (state["base_record_id"],),
            ).fetchone()
            human = c.execute(
                "SELECT title, abstract FROM human_corrections WHERE id=?",
                (state["human_correction_id"],),
            ).fetchone()
            pool = {SOURCE_OCR: live, SOURCE_BASE: base, SOURCE_HUMAN: human}

            updates: dict[str, str] = {}
            detail: dict[str, dict] = {}
            for field in FIELDS:
                if not state[f"{field}_conflict"]:
                    continue  # 自动合并字段不受裁决影响
                if field not in resolutions:
                    raise CorrectionError(
                        f"DOI {doi}：冲突字段 {field} 缺少裁决"
                    )
                source = (resolutions[field] or {}).get("source", "")
                if source not in VALID_RESOLUTION_SOURCES:
                    raise CorrectionError(
                        f"DOI {doi} 字段 {field}：source 必须是 "
                        f"{'/'.join(VALID_RESOLUTION_SOURCES)}，得到 {source!r}"
                    )
                value = pool[source][field]
                updates[field] = value
                detail[field] = {"source": SOURCE_MANUAL,
                                 "chosen_source": source, "value": value}

            # 非冲突字段沿用已自动合并的值；冲突字段以裁决值落库。
            merged_title = updates.get("title", state["merged_title"])
            merged_abstract = updates.get("abstract", state["merged_abstract"])
            title_source = (SOURCE_MANUAL if "title" in updates
                            else state["title_source"])
            abstract_source = (SOURCE_MANUAL if "abstract" in updates
                               else state["abstract_source"])
            batch_id = c.execute(
                "INSERT INTO correction_batches(kind, status, total, detail) "
                "VALUES('resolve','committed',1,?)",
                (json.dumps({"doi": doi, **detail}, ensure_ascii=False),),
            ).lastrowid
            c.execute(
                """
                UPDATE correction_states SET
                    status='clean', merged_title=?, merged_abstract=?,
                    title_source=?, abstract_source=?,
                    title_conflict=0, abstract_conflict=0,
                    conflict_detail=NULL,
                    resolution_ocr_record_id=?, resolution_human_id=?,
                    resolution_detail=?, updated_at=datetime('now')
                WHERE doi=?
                """,
                (merged_title, merged_abstract, title_source, abstract_source,
                 state["live_record_id"], state["human_correction_id"],
                 json.dumps(detail, ensure_ascii=False), doi),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    return {"batch_id": batch_id, "doi": doi, "status": "clean",
            "resolved_fields": sorted(detail.keys())}


def get_state(doi: str) -> dict | None:
    c = conn()
    return _state_payload(c, doi)


def _state_payload(c: sqlite3.Connection, doi: str) -> dict | None:
    state = c.execute(
        "SELECT * FROM correction_states WHERE doi=?", (doi,)
    ).fetchone()
    if state is None:
        return None
    live = c.execute(
        "SELECT id, revision, title, abstract FROM records "
        "WHERE id=? AND superseded=0",
        (state["live_record_id"],),
    ).fetchone()
    base = c.execute(
        "SELECT id, revision, title, abstract FROM records WHERE id=?",
        (state["base_record_id"],),
    ).fetchone()
    human = c.execute(
        "SELECT id, title, abstract, created_at FROM human_corrections "
        "WHERE id=?",
        (state["human_correction_id"],),
    ).fetchone()
    return {
        "doi": doi,
        "status": state["status"],
        "merged_title": state["merged_title"],
        "merged_abstract": state["merged_abstract"],
        "title_source": state["title_source"],
        "abstract_source": state["abstract_source"],
        "title_conflict": bool(state["title_conflict"]),
        "abstract_conflict": bool(state["abstract_conflict"]),
        "conflict_detail": json.loads(state["conflict_detail"])
        if state["conflict_detail"] else None,
        "resolution_ocr_record_id": state["resolution_ocr_record_id"],
        "resolution_human_id": state["resolution_human_id"],
        "resolution_detail": json.loads(state["resolution_detail"])
        if state["resolution_detail"] else None,
        "updated_at": state["updated_at"],
        "live_ocr": dict(live) if live else None,
        "base_ocr": dict(base) if base else None,
        "human": dict(human) if human else None,
    }


def list_states(status_filter: str | None = None, limit: int = 100) -> dict:
    c = conn()
    where = ""
    params: list = []
    if status_filter in ("clean", "conflict"):
        where = "WHERE s.status=?"
        params.append(status_filter)
    rows = c.execute(
        f"""
        SELECT s.doi FROM correction_states s
        {where} ORDER BY s.updated_at DESC, s.doi LIMIT ?
        """,
        (*params, limit),
    ).fetchall()
    items = [_state_payload(c, r["doi"]) for r in rows]
    return {"count": len(items), "results": items}


def counts(c: sqlite3.Connection | None = None) -> dict:
    """供数资格统计：未校订（沿用 OCR）/ clean 可供数 / conflict 暂停。"""
    c = c or conn()
    total_live = c.execute(
        "SELECT COUNT(*) n FROM records WHERE superseded=0"
    ).fetchone()["n"]
    clean = c.execute(
        "SELECT COUNT(*) n FROM correction_states WHERE status='clean'"
    ).fetchone()["n"]
    conflict = c.execute(
        "SELECT COUNT(*) n FROM correction_states WHERE status='conflict'"
    ).fetchone()["n"]
    return {"live_records": total_live, "uncorrected": total_live - clean - conflict,
            "corrected_clean": clean, "conflict_suspended": conflict,
            "supply_eligible": total_live - conflict}


def supply_rows() -> list[sqlite3.Row]:
    """可供数题录快照（单条 SQL 语句 = 同一已提交状态）。

    - clean 状态：取合并后的题名/摘要及来源标注；
    - 无人工版：沿用最新 OCR（source=ocr，human_correction_id 为空）；
    - conflict 状态：整条排除。
    """
    c = conn()
    return c.execute(
        """
        SELECT r.doi, r.revision,
               COALESCE(s.merged_title, r.title)       AS title,
               COALESCE(s.merged_abstract, r.abstract) AS abstract,
               COALESCE(s.title_source, 'ocr')         AS title_source,
               COALESCE(s.abstract_source, 'ocr')      AS abstract_source,
               s.base_record_id      AS ocr_base_record_id,
               r.id                  AS ocr_live_record_id,
               s.human_correction_id AS human_correction_id
        FROM records r
        LEFT JOIN correction_states s
               ON s.doi = r.doi AND s.status = 'clean'
        WHERE r.superseded = 0
          AND NOT EXISTS (
              SELECT 1 FROM correction_states cs
              WHERE cs.doi = r.doi AND cs.status = 'conflict'
          )
        ORDER BY r.doi
        """
    ).fetchall()


EXPORT_HEADER = [
    "doi", "revision", "title", "abstract",
    "title_source", "abstract_source",
    "ocr_base_record_id", "ocr_live_record_id", "human_correction_id",
]


def export_csv() -> str:
    """生成供数 CSV（UTF-8）；整个导出基于 supply_rows 的单语句快照。"""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(EXPORT_HEADER)
    for row in supply_rows():
        writer.writerow([
            row["doi"], row["revision"], row["title"], row["abstract"],
            row["title_source"], row["abstract_source"],
            row["ocr_base_record_id"] if row["ocr_base_record_id"] is not None
            else row["ocr_live_record_id"],
            row["ocr_live_record_id"],
            row["human_correction_id"] if row["human_correction_id"] is not None
            else "",
        ])
    return buf.getvalue()


def recent_batches(limit: int = 20) -> list[dict]:
    c = conn()
    return [dict(r) for r in c.execute(
        "SELECT id, kind, status, total, detail, created_at "
        "FROM correction_batches ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()]

"""替换词典上传与 FTS 索引重建。

发布模型（保证不暴露半成品、构建期不中断服务）：
1. 上传时校验词典并创建新的 FTS 表（空表），旧 FTS/旧词典继续服务检索与导入。
2. 后台线程逐段把现行记录以新词典归一化后灌入新表（短事务，不阻塞写入）。
3. 发布前在单个写事务内做 delta 对账（重建期间被更正/新导入的记录全部补齐），
   再原子切换 meta 中的活动 FTS 指针；随后更新内存映射。
4. 发布失败/中断：活动指针不动，旧索引继续可用，可调用 retry 重试。
"""
from __future__ import annotations

import csv
import io
import sqlite3
import threading
import traceback

from . import state
from .db import conn, create_fts, meta_value, write_lock, FTS_NAME_RE
from .normalize import WORD_HEAD_RE, build_mapping, normalize_text

BATCH_SIZE = 500


class DictValidationError(Exception):
    """词典内容非法（400）。"""


def _parse_dict_csv(content: bytes) -> list[tuple[str, str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DictValidationError(f"词典必须为 UTF-8 编码：{exc}") from exc
    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if any(cell.strip() for cell in r)]
    if not rows:
        raise DictValidationError("词典为空：至少需要一条错词/正词")
    header = [h.strip().lower() for h in rows[0]]
    if header[:2] == ["wrong", "correct"]:
        data_rows = rows[1:]
        if not data_rows:
            raise DictValidationError("词典没有有效条目（仅有表头）")
    else:
        # 无表头：每一行都必须是 合法词,合法词 两列，否则按表头错误拒绝。
        if not all(len(r) >= 2
                   and WORD_HEAD_RE.match(r[0].strip())
                   and WORD_HEAD_RE.match(r[1].strip())
                   for r in rows):
            raise DictValidationError(
                "词典表头必须为 wrong,correct（也可省略表头直接给两列）"
            )
        data_rows = rows

    entries: list[tuple[str, str]] = []
    seen: set[str] = set()
    for idx, row in enumerate(data_rows, start=1):
        if len(row) < 2:
            raise DictValidationError(f"第 {idx} 行：需要 wrong,correct 两列")
        wrong, correct = row[0].strip(), row[1].strip()
        if not WORD_HEAD_RE.match(wrong) or not WORD_HEAD_RE.match(correct):
            raise DictValidationError(
                f"第 {idx} 行：错词/正词只能包含英文字母，得到 "
                f"{wrong!r} -> {correct!r}"
            )
        key = wrong.lower()
        if key in seen:
            raise DictValidationError(
                f"第 {idx} 行：错词重复：{wrong}"
            )
        seen.add(key)
        entries.append((key, correct))

    if not entries:
        raise DictValidationError("词典没有有效条目")

    # 正词不得再作错词：两个集合必须互斥，杜绝链式/循环替换。
    corrects = {c.lower() for _, c in entries}
    overlap = corrects & seen
    if overlap:
        raise DictValidationError(
            "正词不得再作错词，冲突词：" + ", ".join(sorted(overlap))
        )
    return entries


def upload_dictionary(content: bytes, filename: str | None) -> dict:
    entries = _parse_dict_csv(content)

    # 进程内并发守卫：同一当前版只允许一个构建任务。
    if not state.rebuild_lock.acquire(blocking=False):
        from fastapi import HTTPException
        raise HTTPException(
            status_code=409,
            detail="已有词典重建任务在进行，同一当前版只能有一份发布",
        )

    c = conn()
    try:
        with write_lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                base_fts = meta_value(c, "active_fts")
                # 数据库层兜底：存在 building 版本时拒绝新构建（重启恢复后同样生效）。
                busy = c.execute(
                    "SELECT 1 FROM dict_versions WHERE status='building' LIMIT 1"
                ).fetchone()
                if busy:
                    c.execute("ROLLBACK")
                    from fastapi import HTTPException
                    raise HTTPException(
                        status_code=409,
                        detail="数据库中仍有未完成的重建任务，请先重试或等待完成",
                    )
                fts_table = state.next_fts_name(c)
                create_fts(c, fts_table)
                cur = c.execute(
                    "INSERT INTO dict_versions(filename, status, entry_count, "
                    "base_fts, fts_table) VALUES(?,?,?,?,?)",
                    (filename, "building", len(entries), base_fts, fts_table),
                )
                dict_id = cur.lastrowid
                c.executemany(
                    "INSERT INTO dict_entries(dict_version_id, wrong, correct) "
                    "VALUES(?,?,?)",
                    [(dict_id, w, k) for w, k in entries],
                )
                job_id = c.execute(
                    "INSERT INTO rebuild_jobs(dict_version_id, status) "
                    "VALUES(?, 'running')",
                    (dict_id,),
                ).lastrowid
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
    except Exception:
        state.rebuild_lock.release()
        raise

    mapping = build_mapping(entries)
    t = threading.Thread(
        target=_run_rebuild,
        args=(dict_id, job_id, fts_table, mapping),
        name=f"rebuild-{dict_id}",
        daemon=True,
    )
    t.start()
    return {"dict_version_id": dict_id, "rebuild_job_id": job_id,
            "entry_count": len(entries), "fts_table": fts_table,
            "status": "building"}


def _run_rebuild(dict_id: int, job_id: int, fts_table: str,
                 mapping: dict[str, str]) -> None:
    """后台构建主循环；任何异常都会把任务落盘为 failed，活动索引不受影响。"""
    c = conn()
    try:
        assert FTS_NAME_RE.match(fts_table)
        last_id = 0
        while True:
            # 短事务分批灌库，构建期间导入仍可拿写锁完成提交。
            batch = c.execute(
                "SELECT id, title, abstract FROM records "
                "WHERE superseded=0 AND id>? ORDER BY id LIMIT ?",
                (last_id, BATCH_SIZE),
            ).fetchall()
            if not batch:
                break
            c.execute("BEGIN IMMEDIATE")
            try:
                with write_lock:
                    for r in batch:
                        c.execute(
                            f"INSERT INTO {fts_table}(rowid, title_norm, "
                            "abstract_norm) VALUES(?,?,?)",
                            (r["id"], normalize_text(r["title"], mapping),
                             normalize_text(r["abstract"], mapping)),
                            )
                        # 测试故障注入：模拟构建中途崩溃（逐行判定）。
                        if (state.fail_after_id is not None
                                and r["id"] >= state.fail_after_id):
                            state.fail_after_id = None
                            raise RuntimeError(
                                f"注入故障：构建到 rowid {r['id']} 后中断"
                            )
                    last_id = batch[-1]["id"]
                    c.execute(
                        "UPDATE rebuild_jobs SET last_id=? WHERE id=?",
                        (last_id, job_id),
                    )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise

            # 测试同步点（可挂起构建线程，模拟长重建）。
            if state.checkpoint_id is not None and last_id >= state.checkpoint_id:
                ev = state.checkpoint_event
                if ev is not None:
                    ev.set()
                state.checkpoint_event = None
                state.checkpoint_id = None
                if state.hold_event is not None:
                    state.hold_event.wait(30)

        _publish(dict_id, job_id, fts_table, mapping)
    except Exception as exc:  # noqa: BLE001 - 后台线程必须吞掉全部异常
        _mark_failed(c, dict_id, job_id, fts_table, exc)
    finally:
        if state.rebuild_lock.locked():
            state.rebuild_lock.release()


def _publish(dict_id: int, job_id: int, fts_table: str,
             mapping: dict[str, str]) -> None:
    """delta 对账 + 原子切换（单事务），保证新索引含构建期全部已提交修订。"""
    c = conn()
    with write_lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            dv = c.execute(
                "SELECT * FROM dict_versions WHERE id=?", (dict_id,)
            ).fetchone()
            if dv is None or dv["status"] != "building":
                raise RuntimeError(f"词典版本 {dict_id} 状态异常，终止发布")

            # 对账：新表 docid 集合必须与现行 records 集合完全一致。
            wanted = {
                r["id"]: r for r in c.execute(
                    "SELECT id, title, abstract FROM records "
                    "WHERE superseded=0"
                ).fetchall()
            }
            have = {r[0] for r in c.execute(
                f"SELECT rowid FROM {fts_table}"
            ).fetchall()}

            # 构建期被更正/删除的旧行：从新表移除。
            for stale in have - set(wanted):
                c.execute(f"DELETE FROM {fts_table} WHERE rowid=?", (stale,))
            # 构建期新导入或被更正的行：补齐/覆盖。FTS 中已存在的 rowid
            # 直接 INSERT 会被当作更新，故统一先删后插。
            for rid, r in wanted.items():
                if rid in have:
                    c.execute(f"DELETE FROM {fts_table} WHERE rowid=?", (rid,))
                c.execute(
                    f"INSERT INTO {fts_table}(rowid, title_norm, "
                    "abstract_norm) VALUES(?,?,?)",
                    (rid, normalize_text(r["title"], mapping),
                     normalize_text(r["abstract"], mapping)),
                )

            old_fts = meta_value(c, "active_fts")
            base_fts = dv["base_fts"]
            if old_fts != base_fts:
                # 另一版本已抢先发布（跨进程/重启兜底）：本版本放弃。
                c.execute(
                    "UPDATE dict_versions SET status='abandoned' WHERE id=?",
                    (dict_id,),
                )
                c.execute(
                    "UPDATE rebuild_jobs SET status='abandoned', "
                    "finished_at=datetime('now') WHERE id=?",
                    (job_id,),
                )
                c.execute("COMMIT")
                c.execute(f"DROP TABLE IF EXISTS {fts_table}")
                return

            c.execute(
                "UPDATE meta SET value=? WHERE key='active_fts'", (fts_table,)
            )
            c.execute(
                "UPDATE meta SET value=? WHERE key='active_dict'",
                (str(dict_id),),
            )
            c.execute(
                "UPDATE dict_versions SET status='active' WHERE id=?",
                (dict_id,),
            )
            # 先前活动版本转为 superseded（保留条目供审计，仅当前版本可被检索引用）。
            c.execute(
                "UPDATE dict_versions SET status='superseded' "
                "WHERE status='active' AND id<>?",
                (dict_id,),
            )
            c.execute(
                "UPDATE rebuild_jobs SET status='done', "
                "finished_at=datetime('now') WHERE id=?",
                (job_id,),
            )
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

    # 指针已提交后再切换内存映射与清理旧表（旧表已无任何引用）。
    state.set_mapping(mapping)
    try:
        with write_lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(f"DROP TABLE IF EXISTS {old_fts}")
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
    except Exception:
        # 清理失败不影响正确性，下次启动的恢复逻辑会兜底。
        traceback.print_exc()


def _mark_failed(c: sqlite3.Connection, dict_id: int, job_id: int,
                 fts_table: str, exc: Exception) -> None:
    detail = f"{type(exc).__name__}: {exc}"
    traceback.print_exc()
    try:
        with write_lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                c.execute(
                    "UPDATE dict_versions SET status='failed', error=? "
                    "WHERE id=? AND status='building'",
                    (detail, dict_id),
                )
                c.execute(
                    "UPDATE rebuild_jobs SET status='failed', error=?, "
                    "finished_at=datetime('now') WHERE id=?",
                    (detail, job_id),
                )
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
    except Exception:
        traceback.print_exc()


def retry_failed(dict_id: int) -> dict:
    """重试发布前失败/中断的构建：沿用旧索引期间可反复重试。"""
    c = conn()
    dv = c.execute(
        "SELECT * FROM dict_versions WHERE id=?", (dict_id,)
    ).fetchone()
    if dv is None:
        from fastapi import HTTPException
        raise HTTPException(404, f"词典版本 {dict_id} 不存在")
    if dv["status"] not in ("failed", "building"):
        from fastapi import HTTPException
        raise HTTPException(
            409, f"版本状态为 {dv['status']}，仅 failed/building 可重试"
        )
    if not state.rebuild_lock.acquire(blocking=False):
        from fastapi import HTTPException
        raise HTTPException(409, "已有重建任务在进行")

    fts_table = dv["fts_table"]
    try:
        rows = c.execute(
            "SELECT wrong, correct FROM dict_entries WHERE dict_version_id=?",
            (dict_id,),
        ).fetchall()
        mapping = build_mapping(rows)
        with write_lock:
            c.execute("BEGIN IMMEDIATE")
            try:
                # 半成品表清空重建，绝不对外暴露。
                c.execute(f"DROP TABLE IF EXISTS {fts_table}")
                create_fts(c, fts_table)
                c.execute(
                    "UPDATE dict_versions SET status='building', error=NULL "
                    "WHERE id=?",
                    (dict_id,),
                )
                job_id = c.execute(
                    "INSERT INTO rebuild_jobs(dict_version_id, status) "
                    "VALUES(?, 'running')",
                    (dict_id,),
                ).lastrowid
                c.execute("COMMIT")
            except Exception:
                c.execute("ROLLBACK")
                raise
    except Exception:
        state.rebuild_lock.release()
        raise

    threading.Thread(
        target=_run_rebuild,
        args=(dict_id, job_id, fts_table, mapping),
        name=f"rebuild-retry-{dict_id}",
        daemon=True,
    ).start()
    return {"dict_version_id": dict_id, "rebuild_job_id": job_id,
            "status": "building"}


def recover_on_startup() -> dict:
    """重启恢复：
    - 中断的 building/failed 任务保持 failed，旧索引继续服务，可重试；
    - 活动 FTS 若缺失（极端情况）则按活动词典重建；
    - 清理所有未被引用的孤儿 fts_v* 表。
    """
    c = conn()
    result = {"abandoned_jobs": [], "orphan_fts": [], "active_rebuilt": False}
    with write_lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            c.execute(
                "UPDATE dict_versions SET status='failed', "
                "error=COALESCE(error,'进程中断，重启后标记失败，可重试') "
                "WHERE status='building'"
            )
            c.execute(
                "UPDATE rebuild_jobs SET status='failed', "
                "error=COALESCE(error,'进程中断，重启后标记失败，可重试'), "
                "finished_at=datetime('now') WHERE status IN ('running','done') "
                "AND dict_version_id IN "
                "(SELECT id FROM dict_versions WHERE status='failed')"
            )
            # 真正卡在 running 的作业同样落 failed。
            c.execute(
                "UPDATE rebuild_jobs SET status='failed', "
                "error=COALESCE(error,'进程中断，重启后标记失败，可重试'), "
                "finished_at=datetime('now') WHERE status='running'"
            )
            for r in c.execute(
                "SELECT id FROM dict_versions WHERE status='failed'"
            ).fetchall():
                result["abandoned_jobs"].append(r["id"])

            # 新进程启动时没有任何活动后台线程，进程内锁必须复位。
            if state.rebuild_lock.locked():
                state.rebuild_lock.release()

            active_fts = meta_value(c, "active_fts")
            if not state.table_exists(c, active_fts):
                active_dict = int(meta_value(c, "active_dict"))
                create_fts(c, active_fts)
                if active_dict:
                    rows = c.execute(
                        "SELECT wrong, correct FROM dict_entries "
                        "WHERE dict_version_id=?",
                        (active_dict,),
                    ).fetchall()
                    mapping = build_mapping(rows)
                else:
                    mapping = {}
                for r in c.execute(
                    "SELECT id, title, abstract FROM records "
                    "WHERE superseded=0"
                ).fetchall():
                    c.execute(
                        f"INSERT INTO {active_fts}(rowid, title_norm, "
                        "abstract_norm) VALUES(?,?,?)",
                        (r["id"], normalize_text(r["title"], mapping),
                         normalize_text(r["abstract"], mapping)),
                    )
                result["active_rebuilt"] = True

            referenced = {meta_value(c, "active_fts")}
            for r in c.execute(
                "SELECT fts_table FROM dict_versions WHERE status IN "
                "('building','failed')"
            ).fetchall():
                # 并集：活动表绝不能被当作孤儿表清理。
                referenced.add(r["fts_table"])
            # 只匹配 FTS 主表名（fts_v数字），绝不能误伤 fts_v1_data 等影子表。
            masters = [
                r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name GLOB 'fts_v[0-9]*'"
                ).fetchall()
            ]
            for name in masters:
                if not FTS_NAME_RE.match(name):
                    continue
                if name not in referenced:
                    c.execute(f"DROP TABLE IF EXISTS {name}")
                    result["orphan_fts"].append(name)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return result

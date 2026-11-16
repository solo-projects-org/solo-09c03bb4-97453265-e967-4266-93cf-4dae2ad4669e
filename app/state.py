"""进程内活动状态：活动词典映射缓存、重建并发控制。"""
from __future__ import annotations

import sqlite3
import threading

from .db import conn, meta_value
from .normalize import build_mapping

#: 重建期间检索沿用的「旧词典」；发布成功后整体切换。
_mapping: dict[str, str] = {}
_mapping_lock = threading.Lock()
#: 同一时刻只允许一个重建任务（同一当前版并发换词典只能一份发布）。
rebuild_lock = threading.Lock()

#: 测试用故障注入：构建到该 rowid 时抛出异常（None 表示关闭）。
fail_after_id: int | None = None
#: 测试用同步点：处理完该 rowid 后回调一次（用于构造构建期间的并发导入）。
checkpoint_id: int | None = None
checkpoint_event: threading.Event | None = None
#: 测试用挂起点：事件触发后阻塞重建，直到该事件被 set（None 表示不挂起）。
hold_event: threading.Event | None = None


def current_mapping() -> dict[str, str]:
    with _mapping_lock:
        return dict(_mapping)


def set_mapping(mapping: dict[str, str]) -> None:
    with _mapping_lock:
        _mapping.clear()
        _mapping.update(mapping)


def load_active_mapping() -> None:
    """启动/发布后从已提交的 dict_entries 重建内存映射。"""
    c = conn()
    active = int(meta_value(c, "active_dict"))
    if active == 0:
        set_mapping({})
        return
    rows = c.execute(
        "SELECT wrong, correct FROM dict_entries WHERE dict_version_id=?",
        (active,),
    ).fetchall()
    set_mapping(build_mapping(rows))


def active_fts_name() -> str:
    return meta_value(conn(), "active_fts")


def next_fts_name(c: sqlite3.Connection) -> str:
    seq = int(meta_value(c, "fts_seq")) + 1
    c.execute("UPDATE meta SET value=? WHERE key='fts_seq'", (str(seq),))
    return f"fts_v{seq}"


def table_exists(c: sqlite3.Connection, name: str) -> bool:
    return c.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?",
        (name,),
    ).fetchone() is not None

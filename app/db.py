"""SQLite 连接管理与 schema 初始化。

设计要点：
- WAL 模式：读写并发，重建索引期间检索不阻塞。
- ``isolation_level=None``（autocommit）+ 显式 BEGIN/COMMIT，事务边界清晰。
- 进程内 ``write_lock`` 串行化所有写事务（compose 固定单 worker），
  ``busy_timeout`` 作为跨进程兜底。
"""
from __future__ import annotations

import os
import sqlite3
import threading

#: 串行化本进程内所有写操作（导入、词典发布、恢复）。
write_lock = threading.RLock()

_path = os.environ.get("DB_PATH", os.path.join("data", "app.db"))
_local = threading.local()

INITIAL_FTS = "fts_v1"
FTS_NAME_RE = __import__("re").compile(r"^fts_v\d+$")

SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS meta(
        key   TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS records(
        id              INTEGER PRIMARY KEY,
        doi             TEXT NOT NULL,
        revision        INTEGER NOT NULL,
        title           TEXT NOT NULL,
        abstract        TEXT NOT NULL DEFAULT '',
        content_hash    TEXT NOT NULL,
        dict_version_id INTEGER,
        superseded      INTEGER NOT NULL DEFAULT 0,
        created_at      TEXT NOT NULL DEFAULT (datetime('now')),
        UNIQUE(doi, revision, content_hash)
    )
    """,
    # 每个 DOI 至多一条现行（未被取代）记录。
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_records_doi_live "
    "ON records(doi) WHERE superseded=0",
    "CREATE INDEX IF NOT EXISTS idx_records_superseded ON records(superseded)",
    """
    CREATE TABLE IF NOT EXISTS import_batches(
        id          INTEGER PRIMARY KEY,
        filename    TEXT,
        status      TEXT NOT NULL,            -- accepted / reused / rejected
        total       INTEGER NOT NULL,
        accepted    INTEGER NOT NULL DEFAULT 0,
        reused      INTEGER NOT NULL DEFAULT 0,
        detail      TEXT NOT NULL,            -- JSON 逐行说明
        received_at TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dict_versions(
        id          INTEGER PRIMARY KEY,
        filename    TEXT,
        status      TEXT NOT NULL,            -- building / active / failed / abandoned
        entry_count INTEGER NOT NULL,
        base_fts    TEXT NOT NULL,            -- 创建时的现行 FTS（发布并发守卫）
        fts_table   TEXT NOT NULL,
        error       TEXT,
        created_at  TEXT NOT NULL DEFAULT (datetime('now'))
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS dict_entries(
        dict_version_id INTEGER NOT NULL REFERENCES dict_versions(id),
        wrong           TEXT NOT NULL,
        correct         TEXT NOT NULL,
        PRIMARY KEY(dict_version_id, wrong)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS rebuild_jobs(
        id              INTEGER PRIMARY KEY,
        dict_version_id INTEGER NOT NULL REFERENCES dict_versions(id),
        status          TEXT NOT NULL,        -- running / done / failed / abandoned
        last_id         INTEGER NOT NULL DEFAULT 0,
        error           TEXT,
        started_at      TEXT NOT NULL DEFAULT (datetime('now')),
        finished_at     TEXT
    )
    """,
]


def set_path(path: str) -> None:
    global _path
    _path = path
    # 切换路径后丢弃本线程旧连接。
    if hasattr(_local, "conn"):
        try:
            _local.conn.close()
        except Exception:
            pass
        del _local.conn


def conn() -> sqlite3.Connection:
    """返回当前线程独占的连接（线程局部，禁止跨线程共享）。"""
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(os.path.abspath(_path)), exist_ok=True)
        c = sqlite3.connect(_path, isolation_level=None, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=30000")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


def meta_value(c: sqlite3.Connection, key: str) -> str:
    row = c.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    if row is None:
        raise RuntimeError(f"meta key missing: {key}")
    return row[0]


def create_fts(c: sqlite3.Connection, name: str) -> None:
    assert FTS_NAME_RE.match(name), f"非法 FTS 表名: {name}"
    # 普通内容型 FTS5（非 contentless），可按 rowid 正常 INSERT/DELETE，
    # docid 与 records.id 一一对应。
    c.execute(
        f"CREATE VIRTUAL TABLE {name} USING fts5("
        "title_norm, abstract_norm, tokenize='unicode61 remove_diacritics 2')"
    )


def init_schema() -> None:
    c = conn()
    with write_lock:
        c.execute("BEGIN IMMEDIATE")
        try:
            for ddl in SCHEMA:
                c.execute(ddl)
            row = c.execute(
                "SELECT 1 FROM meta WHERE key='active_fts'"
            ).fetchone()
            if row is None:
                create_fts(c, INITIAL_FTS)
                c.execute(
                    "INSERT INTO meta(key,value) VALUES('active_fts',?)",
                    (INITIAL_FTS,),
                )
                # 0 表示恒等映射（无词典）。
                c.execute("INSERT INTO meta(key,value) VALUES('active_dict','0')")
                c.execute("INSERT INTO meta(key,value) VALUES('fts_seq','1')")
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise

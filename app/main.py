"""FastAPI 入口：题录上传、词典上传/重试、检索、状态、操作网页。"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from . import records as records_mod
from . import rebuild as rebuild_mod
from . import search as search_mod
from . import state
from .db import conn, init_schema, meta_value


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_schema()
    app.state.recovery = rebuild_mod.recover_on_startup()
    state.load_active_mapping()
    yield


app = FastAPI(title="OCR 题录纠错检索系统", version="1.0.0",
              lifespan =lifespan)

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
MAX_UPLOAD = 20 * 1024 * 1024  # 20 MB


@app.get("/")
def index() -> FileResponse:
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get("/api/status")
def get_status() -> dict:
    c = conn()
    active_dict = int(meta_value(c, "active_dict"))
    active_fts = meta_value(c, "active_fts")
    record_count = c.execute(
        "SELECT COUNT(*) n FROM records WHERE superseded=0"
    ).fetchone()["n"]
    superseded = c.execute(
        "SELECT COUNT(*) n FROM records WHERE superseded=1"
    ).fetchone()["n"]
    dicts = [
        dict(r) for r in c.execute(
            "SELECT id, status, entry_count, fts_table, base_fts, error, "
            "created_at FROM dict_versions ORDER BY id DESC LIMIT 20"
        ).fetchall()
    ]
    jobs = [
        dict(r) for r in c.execute(
            "SELECT id, dict_version_id, status, last_id, error, "
            "started_at, finished_at FROM rebuild_jobs "
            "ORDER BY id DESC LIMIT 20"
        ).fetchall()
    ]
    batches = [
        dict(r) for r in c.execute(
            "SELECT id, filename, status, total, accepted, reused, "
            "received_at FROM import_batches ORDER BY id DESC LIMIT 20"
        ).fetchall()
    ]
    return {
        "active_dict_version_id": active_dict,
        "active_fts": active_fts,
        "current_mapping": state.current_mapping(),
        "record_count": record_count,
        "superseded_count": superseded,
        "rebuild_in_progress": state.rebuild_lock.locked(),
        "dict_versions": dicts,
        "rebuild_jobs": jobs,
        "import_batches": batches,
        "startup_recovery": getattr(app.state, "recovery", None),
    }


@app.post("/api/records/import")
async def import_records(file: UploadFile = File(...)) -> JSONResponse:
    content = await file.read()
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "文件超过 20MB 限制")
    try:
        result = records_mod.import_records_csv(content, file.filename)
    except records_mod.ImportError_ as exc:
        # 拒收批次也留痕（不写任何 records/FTS 数据）。
        c = conn()
        import json
        c.execute(
            "INSERT INTO import_batches(filename, status, total, detail) "
            "VALUES(?,?,?,?)",
            (file.filename, "rejected", 0,
             json.dumps([str(exc)], ensure_ascii=False)),
        )
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(result, status_code=201)


@app.post("/api/dictionaries")
async def upload_dictionary(file: UploadFile = File(...)) -> JSONResponse:
    content = await file.read()
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, "文件超过 20MB 限制")
    try:
        result = rebuild_mod.upload_dictionary(content, file.filename)
    except rebuild_mod.DictValidationError as exc:
        raise HTTPException(400, str(exc)) from exc
    return JSONResponse(result, status_code=202)


@app.post("/api/dictionaries/{dict_id}/retry")
def retry_dictionary(dict_id: int) -> dict:
    return rebuild_mod.retry_failed(dict_id)


@app.get("/api/search")
def do_search(q: str = Query("", description="查询词，空格分隔，全部命中"),
              limit: int = Query(100, ge=1, le=500)) -> dict:
    return search_mod.search(q, limit)


@app.get("/api/records")
def list_records(include_superseded: bool = False,
                 limit: int = Query(100, ge=1, le=500)) -> dict:
    c = conn()
    where = "" if include_superseded else "WHERE superseded=0"
    rows = c.execute(
        f"SELECT id, doi, revision, title, abstract, dict_version_id, "
        f"superseded, created_at FROM records {where} "
        "ORDER BY doi, revision DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return {"count": len(rows), "results": [dict(r) for r in rows]}

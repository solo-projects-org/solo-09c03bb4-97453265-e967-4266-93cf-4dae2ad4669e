"""恢复路径与词典格式边界。"""
from conftest import dict_csv, make_csv, wait_until  # type: ignore
from app import state  # type: ignore
from app.db import conn, meta_value  # type: ignore


def test_headerless_dictionary_accepted(client):
    client.post("/api/records/import", files={"file": ("a.csv", make_csv([
        {"doi": "10.1/h", "revision": 1,
         "title": "teh system", "abstract": ""},
    ]))})
    raw = b"teh,the\n"  # 无表头，两列纯词条
    r = client.post("/api/dictionaries", files={"file": ("d.csv", raw)})
    assert r.status_code == 202
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])
    assert client.get("/api/search", params={"q": "the"}).json()["count"] == 1


def test_bad_header_rejected(client):
    # 表头既不是 wrong,correct，数据行又含非法词形（数字），无法按无表头词典接受。
    r = client.post(
        "/api/dictionaries",
        files={"file": ("d.csv", b"bad,header\nt3h,the\n")},
    )
    assert r.status_code == 400 and "wrong,correct" in r.json()["detail"]


def test_orphan_fts_cleaned_on_restart(client):
    client.post("/api/records/import", files={"file": ("a.csv", make_csv([
        {"doi": "10.1/o", "revision": 1, "title": "teh data", "abstract": ""},
    ]))})
    r = client.post("/api/dictionaries",
                    files={"file": ("d.csv", dict_csv([("teh", "the")]))})
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])

    c = conn()
    active = meta_value(c, "active_fts")
    # 手工制造一个孤儿 FTS 表（含影子表）。
    c.execute("CREATE VIRTUAL TABLE fts_v99 USING fts5(x)")
    assert c.execute(
        "SELECT 1 FROM sqlite_master WHERE name='fts_v99'").fetchone()

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client2:
        recovery = client2.get("/api/status").json()["startup_recovery"]
        assert "fts_v99" in recovery["orphan_fts"]
        c2 = conn()
        assert c2.execute(
            "SELECT 1 FROM sqlite_master WHERE name='fts_v99'").fetchone() is None
        # 活动表及其影子表完好
        assert c2.execute(
            "SELECT 1 FROM sqlite_master WHERE name=?", (active,)).fetchone()
        assert client2.get("/api/search",
                           params={"q": "the"}).json()["count"] == 1


def test_missing_active_fts_rebuilt_on_restart(client):
    client.post("/api/records/import", files={"file": ("a.csv", make_csv([
        {"doi": "10.1/m", "revision": 1, "title": "teh recover", "abstract": ""},
    ]))})
    c = conn()
    active = meta_value(c, "active_fts")
    c.execute(f"DROP TABLE {active}")  # 极端情况：活动表丢失

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client2:
        recovery = client2.get("/api/status").json()["startup_recovery"]
        assert recovery["active_rebuilt"] is True
        assert client2.get("/api/search",
                           params={"q": "teh"}).json()["count"] == 1


def test_rejected_batch_leaves_no_data_and_is_audited(client):
    r = client.post("/api/records/import", files={"file": ("bad.csv", make_csv([
        {"doi": "10.1/z", "revision": 1, "title": "ok", "abstract": ""},
        {"doi": "10.1/z", "revision": 2, "title": "dup", "abstract": ""},
    ]))})
    assert r.status_code == 400
    status = client.get("/api/status").json()
    assert status["record_count"] == 0
    assert status["import_batches"][0]["status"] == "rejected"

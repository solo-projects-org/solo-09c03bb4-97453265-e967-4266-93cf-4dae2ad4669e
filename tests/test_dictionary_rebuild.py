"""词典校验、重建发布、构建期并发导入、故障恢复与并发守卫。"""
import threading

from conftest import dict_csv, make_csv, wait_until  # type: ignore
from app import state  # type: ignore


def seed_records(client, n=6):
    rows = [
        {"doi": f"10.1/d{i}", "revision": 1,
         "title": f"Teh quantum report number {i}",
         "abstract": f"machne learning study {i}"}
        for i in range(1, n + 1)
    ]
    r = client.post("/api/records/import",
                    files={"file": ("seed.csv", make_csv(rows))})
    assert r.status_code == 201
    return rows


def test_dictionary_validation(client):
    # 正词又当错词 -> 拒绝
    r = client.post("/api/dictionaries",
                    files={"file": ("d.csv", dict_csv([("teh", "the"),
                                                        ("the", "teh")]))})
    assert r.status_code == 400 and "正词不得再作错词" in r.json()["detail"]

    # 重复错词 -> 拒绝
    r = client.post("/api/dictionaries",
                    files={"file": ("d.csv", dict_csv([("teh", "the"),
                                                        ("teh", "tha")]))})
    assert r.status_code == 400 and "重复" in r.json()["detail"]

    # 非法字符 -> 拒绝
    r = client.post("/api/dictionaries",
                    files={"file": ("d.csv", dict_csv([("t3h", "the")]))})
    assert r.status_code == 400

    # 拒绝后状态保持无词典
    assert client.get("/api/status").json()["active_dict_version_id"] == 0


def test_rebuild_publishes_and_repairs_ocr_misses(client):
    seed_records(client)
    # 无词典时，正确拼写 the 检不到 OCR 错词 teh
    assert client.get("/api/search", params={"q": "the"}).json()["count"] == 0

    r = client.post("/api/dictionaries",
                    files={"file": ("dict.csv",
                                    dict_csv([("teh", "the"),
                                              ("machne", "machine")]))})
    assert r.status_code == 202
    dict_id = r.json()["dict_version_id"]

    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])
    status = client.get("/api/status").json()
    assert status["active_dict_version_id"] == dict_id
    v = next(v for v in status["dict_versions"] if v["id"] == dict_id)
    assert v["status"] == "active"

    # 错词造成的漏检被修复，且查询词也走同一归一规则
    data = client.get("/api/search", params={"q": "the machine"}).json()
    assert data["count"] == 6
    assert data["normalized_terms"] == ["the", "machine"]
    assert data["dict_version_id"] == dict_id
    # 原题名保持原样返回
    assert data["results"][0]["title"].startswith("Teh quantum")
    # 用原始错词查同样命中（查询归一）
    assert client.get("/api/search", params={"q": "teh"}).json()["count"] == 6


def test_case_insensitive_whole_word_only(client):
    client.post("/api/records/import", files={"file": ("a.csv", make_csv([
        {"doi": "10.1/w", "revision": 1,
         "title": "TEH teh tehin technical", "abstract": ""},
    ]))})
    client.post("/api/dictionaries",
                files={"file": ("d.csv", dict_csv([("teh", "the")]))})
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])

    assert client.get("/api/search", params={"q": "the"}).json()["count"] == 1
    # tehin 是另一个词，不得被部分替换
    assert client.get("/api/search", params={"q": "tehin"}).json()["count"] == 1


def test_import_during_rebuild_is_included_in_published_index(client):
    seed_records(client, n=5)  # BATCH_SIZE=2 -> 多批
    # 检查点：处理完前 2 条后挂起重建，放行一个并发导入。
    ev = threading.Event()
    hold = threading.Event()
    state.checkpoint_id = 2
    state.checkpoint_event = ev
    state.hold_event = hold

    r = client.post("/api/dictionaries",
                    files={"file": ("d.csv", dict_csv([("teh", "the")]))})
    assert r.status_code == 202
    assert ev.wait(10), "重建检查点未触发"

    # 构建期间：旧词典仍服务检索；新导入立即可在当前索引检索。
    before = client.get("/api/search", params={"q": "teh"}).json()
    assert before["count"] == 5
    mid = client.post("/api/records/import",
                      files={"file": ("mid.csv", make_csv([
                          {"doi": "10.1/mid", "revision": 1,
                           "title": "Teh midstream insert", "abstract": ""},
                      ]))})
    assert mid.status_code == 201
    assert client.get("/api/search", params={"q": "midstream"}).json()["count"] == 1

    hold.set()  # 放行重建
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])
    # 发布后的新索引必须包含构建期间提交的全部记录
    data = client.get("/api/search", params={"q": "the"}).json()
    assert data["count"] == 6
    dois = {x["doi"] for x in data["results"]}
    assert "10.1/mid" in dois


def test_supersede_during_rebuild_is_caught_up(client):
    seed_records(client, n=5)
    ev = threading.Event()
    hold = threading.Event()
    state.checkpoint_id = 2
    state.checkpoint_event = ev
    state.hold_event = hold
    client.post("/api/dictionaries",
                files={"file": ("d.csv", dict_csv([("teh", "the")]))})
    assert ev.wait(10)

    # 构建期间高修订更正：旧行必须在发布前从新表 delta 对账删除。
    r = client.post("/api/records/import",
                    files={"file": ("fix.csv", make_csv([
                        {"doi": "10.1/d1", "revision": 2,
                         "title": "The corrected quantum report", "abstract": ""},
                    ]))})
    assert r.status_code == 201
    hold.set()
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])

    data = client.get("/api/search", params={"q": "the"}).json()
    dois = {x["doi"]: x["revision"] for x in data["results"]}
    assert dois.get("10.1/d1") == 2
    assert len(dois) == 5  # 没有重复/幽灵行


def test_build_failure_keeps_old_index_and_retry_works(client):
    seed_records(client, n=5)
    # 第一次词典发布成功，作为“旧索引/旧词典”。
    r = client.post("/api/dictionaries",
                    files={"file": ("v1.csv", dict_csv([("teh", "the")]))})
    assert r.status_code == 202
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])
    v1 = client.get("/api/status").json()["active_dict_version_id"]

    # 第二版构建注入故障。
    state.fail_after_id = 3
    r = client.post("/api/dictionaries",
                    files={"file": ("v2.csv",
                                    dict_csv([("machne", "machine")]))})
    assert r.status_code == 202
    v2 = r.json()["dict_version_id"]
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])

    status = client.get("/api/status").json()
    assert status["active_dict_version_id"] == v1  # 旧版仍在服务
    failed = next(v for v in status["dict_versions"] if v["id"] == v2)
    assert failed["status"] == "failed" and failed["error"]
    # 旧检索完全可用
    assert client.get("/api/search", params={"q": "the"}).json()["count"] == 5

    # 关闭故障注入后重试，发布成功
    state.fail_after_id = None
    r = client.post(f"/api/dictionaries/{v2}/retry")
    assert r.status_code == 200
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])
    status = client.get("/api/status").json()
    assert status["active_dict_version_id"] == v2
    assert client.get("/api/search",
                      params={"q": "machine"}).json()["count"] == 5


def test_concurrent_dictionary_upload_conflict(client):
    seed_records(client, n=6)
    ev = threading.Event()
    hold = threading.Event()
    state.checkpoint_id = 4
    state.checkpoint_event = ev
    state.hold_event = hold
    r1 = client.post("/api/dictionaries",
                     files={"file": ("a.csv", dict_csv([("teh", "the")]))})
    assert r1.status_code == 202
    assert ev.wait(10)
    r2 = client.post("/api/dictionaries",
                     files={"file": ("b.csv", dict_csv([("machne", "machine")]))})
    assert r2.status_code == 409
    hold.set()
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])


def test_retry_rebuilds_full_index_without_missing_rows(client):
    # 回归：重试不得沿用失败作业的 last_id，新索引必须覆盖全部现行记录。
    seed_records(client, n=5)
    state.fail_after_id = 3
    r = client.post("/api/dictionaries",
                    files={"file": ("d.csv", dict_csv([("teh", "the")]))})
    vid = r.json()["dict_version_id"]
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])
    state.fail_after_id = None
    assert client.post(f"/api/dictionaries/{vid}/retry").status_code == 200
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])
    data = client.get("/api/search", params={"q": "the"}).json()
    assert data["count"] == 5
    assert len({x["doi"] for x in data["results"]}) == 5


def test_restart_recovery_marks_interrupted_and_keeps_old_index(client):
    seed_records(client, n=5)
    ev = threading.Event()
    hold = threading.Event()
    state.checkpoint_id = 2
    state.checkpoint_event = ev
    state.hold_event = hold
    r = client.post("/api/dictionaries",
                    files={"file": ("d.csv", dict_csv([("teh", "the")]))})
    dict_id = r.json()["dict_version_id"]
    assert ev.wait(10)
    # 确认作业确实处于构建中，再模拟崩溃。
    assert client.get("/api/status").json()["rebuild_in_progress"] is True

    # 模拟进程崩溃：直接重建应用（不等待后台线程），新 TestClient 走 startup 恢复。
    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client2:
        status = client2.get("/api/status").json()
        assert status["active_dict_version_id"] == 0  # 旧版（无词典）继续服务
        v = next(x for x in status["dict_versions"] if x["id"] == dict_id)
        assert v["status"] == "failed"
        assert status["rebuild_in_progress"] is False
        # 旧索引可用且数据完整
        assert client2.get("/api/search",
                           params={"q": "teh"}).json()["count"] == 5
        # 可重试并成功
        r2 = client2.post(f"/api/dictionaries/{dict_id}/retry")
        assert r2.status_code == 200
        wait_until(lambda: not client2.get("/api/status").json()["rebuild_in_progress"])
        assert client2.get("/api/search",
                           params={"q": "the"}).json()["count"] == 5

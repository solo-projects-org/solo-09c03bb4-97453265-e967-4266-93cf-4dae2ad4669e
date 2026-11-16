"""题录导入规则：高修订更正、同版复用、同版异文/旧版整批拒收。"""
from conftest import make_csv  # type: ignore


def test_basic_import_and_identity_search(client):
    csv = make_csv([
        {"doi": "10.1/a", "revision": 1,
         "title": "Quantum teh optics", "abstract": "photon study"},
        {"doi": "10.1/b", "revision": 1,
         "title": "Machine learning", "abstract": "neural nets"},
    ])
    r = client.post("/api/records/import", files={"file": ("t.csv", csv)})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["total"] == 2 and body["accepted"] == 2

    r = client.get("/api/search", params={"q": "quantum"})
    assert r.status_code == 200
    data = r.json()
    assert data["count"] == 1
    assert data["results"][0]["doi"] == "10.1/a"
    assert data["results"][0]["title"] == "Quantum teh optics"
    assert data["dict_version_id"] == 0  # 无词典状态


def test_higher_revision_supersedes(client):
    csv1 = make_csv([{"doi": "10.1/x", "revision": 1,
                      "title": "Old teh title", "abstract": "alpha text"}])
    client.post("/api/records/import", files={"file": ("a.csv", csv1)})
    csv2 = make_csv([{"doi": "10.1/x", "revision": 2,
                      "title": "New the title", "abstract": "beta text"}])
    r = client.post("/api/records/import", files={"file": ("b.csv", csv2)})
    assert r.json()["accepted"] == 1

    # 旧文不再可检，只返回现行版本。
    hits = client.get("/api/search", params={"q": "alpha"}).json()
    assert hits["count"] == 0
    hits = client.get("/api/search", params={"q": "beta"}).json()
    assert hits["count"] == 1 and hits["results"][0]["revision"] == 2

    status = client.get("/api/status").json()
    assert status["record_count"] == 1
    assert status["superseded_count"] == 1


def test_same_revision_same_content_reused(client):
    row = {"doi": "10.1/r", "revision": 3, "title": "Same title",
           "abstract": "same abs"}
    client.post("/api/records/import",
                files={"file": ("a.csv", make_csv([row]))})
    r = client.post("/api/records/import",
                    files={"file": ("b.csv", make_csv([row]))})
    body = r.json()
    assert body["status"] == "reused" and body["reused"] == 1
    assert client.get("/api/status").json()["record_count"] == 1


def test_same_revision_different_content_rejects_whole_batch(client):
    client.post("/api/records/import", files={"file": ("a.csv", make_csv([
        {"doi": "10.1/m", "revision": 1, "title": "First", "abstract": ""},
    ]))})
    # 第二条是有效的高修订，但第一条同版异文 -> 整批回滚。
    r = client.post("/api/records/import", files={"file": ("bad.csv", make_csv([
        {"doi": "10.1/m", "revision": 1, "title": "Changed", "abstract": ""},
        {"doi": "10.1/n", "revision": 1, "title": "Brand new", "abstract": ""},
    ]))})
    assert r.status_code == 400 and "同版异文" in r.json()["detail"]
    status = client.get("/api/status").json()
    assert status["record_count"] == 1  # 第二条未泄漏
    assert status["import_batches"][0]["status"] == "rejected"


def test_lower_revision_rejects_batch(client):
    client.post("/api/records/import", files={"file": ("a.csv", make_csv([
        {"doi": "10.1/l", "revision": 5, "title": "Fifth", "abstract": ""},
    ]))})
    r = client.post("/api/records/import", files={"file": ("old.csv", make_csv([
        {"doi": "10.1/l", "revision": 4, "title": "Fourth", "abstract": ""},
    ]))})
    assert r.status_code == 400 and "低于现行" in r.json()["detail"]


def test_in_batch_duplicate_doi_rejects(client):
    r = client.post("/api/records/import", files={"file": ("dup.csv", make_csv([
        {"doi": "10.1/d", "revision": 1, "title": "A", "abstract": ""},
        {"doi": "10.1/d", "revision": 2, "title": "B", "abstract": ""},
    ]))})
    assert r.status_code == 400 and "重复" in r.json()["detail"]
    assert client.get("/api/status").json()["record_count"] == 0


def test_missing_columns_and_optional_abstract(client):
    r = client.post(
        "/api/records/import",
        files={"file": ("x.csv", b"doi,revision\n10.1/z,1\n")},
    )
    assert r.status_code == 400 and "题名" in r.json()["detail"]

    r = client.post("/api/records/import", files={"file": ("ok.csv", make_csv(
        [{"doi": "10.1/z", "revision": 1, "title": "No abstract here"}],
        columns=["doi", "revision", "title"],
    ))})
    assert r.status_code == 201
    assert client.get("/api/search", params={"q": "abstract"}).json()["count"] == 1


def test_all_query_terms_must_match(client):
    client.post("/api/records/import", files={"file": ("a.csv", make_csv([
        {"doi": "10.1/q", "revision": 1,
         "title": "alpha beta gamma", "abstract": ""},
    ]))})
    assert client.get("/api/search", params={"q": "alpha gamma"}).json()["count"] == 1
    assert client.get("/api/search", params={"q": "alpha delta"}).json()["count"] == 0

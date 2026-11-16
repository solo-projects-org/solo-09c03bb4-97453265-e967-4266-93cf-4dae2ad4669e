"""人工校订：三方合并矩阵、冲突暂停/裁决、批次原子性、重启持久化、供数导出。"""
import csv
import io

from conftest import make_csv, wait_until  # type: ignore


def import_one(client, doi, revision=1, title="OCR base title",
               abstract="ocr base abstract"):
    r = client.post("/api/records/import", files={"file": ("r.csv", make_csv([
        {"doi": doi, "revision": revision, "title": title,
         "abstract": abstract},
    ]))})
    assert r.status_code == 201, r.text
    return r.json()


def submit(client, items, expected=201):
    r = client.post("/api/corrections", json={"items": items})
    assert r.status_code == expected, r.text
    return r.json()


def state_of(client, doi):
    r = client.get("/api/corrections/" + doi)
    assert r.status_code == 200, r.text
    return r.json()


def export_rows(client):
    r = client.get("/api/export/supply.csv")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/csv")
    return list(csv.DictReader(io.StringIO(r.text)))


def export_map(client):
    return {r["doi"]: r for r in export_rows(client)}


# ---------- 基本提交与供数 ----------

def test_submit_correction_creates_state_and_supply_uses_merged(client):
    import_one(client, "10.1/a", title="Teh OCR title", abstract="ocr abs")
    d = submit(client, [{"doi": "10.1/a", "title": "The human title",
                         "abstract": "human abs"}])
    assert d["status"] == "committed" and d["auto_merged"] == 1
    s = state_of(client, "10.1/a")
    assert s["status"] == "clean"
    assert s["merged_title"] == "The human title"
    assert s["title_source"] == "human"
    # 基底与现行 OCR 相同：来源标 base/human 的等价路径
    assert s["base_ocr"]["id"] == s["live_ocr"]["id"]

    rows = export_map(client)
    row = rows["10.1/a"]
    assert row["title"] == "The human title"
    assert row["abstract"] == "human abs"
    assert row["title_source"] == "human"
    assert row["human_correction_id"] == str(s["human"]["id"])
    assert row["ocr_base_record_id"] == str(s["base_ocr"]["id"])


def test_uncorrected_record_follows_latest_ocr_in_export(client):
    import_one(client, "10.1/u", title="First OCR", abstract="v1 abs")
    import_one(client, "10.1/u", revision=2, title="Second OCR",
               abstract="v2 abs")
    row = export_map(client)["10.1/u"]
    assert row["title"] == "Second OCR"
    assert row["abstract"] == "v2 abs"
    assert row["title_source"] == "ocr"
    assert row["human_correction_id"] == ""


def test_submit_validation_and_unknown_doi(client):
    r = client.post("/api/corrections", json={"items": []})
    assert r.status_code == 400  # 空数组由服务层拒绝
    r = client.post("/api/corrections",
                    json={"items": [{"doi": "  ", "title": "x"}]})
    assert r.status_code == 400 and "DOI" in r.json()["detail"]
    r = client.post("/api/corrections",
                    json={"items": [{"doi": "10.1/x", "title": "  "}]})
    assert r.status_code == 400 and "题名" in r.json()["detail"]
    r = client.post("/api/corrections", json={"items": [
        {"doi": "10.1/gone", "title": "t"},
        {"doi": "10.1/gone", "title": "t2"},
    ]})
    assert r.status_code == 400 and "重复" in r.json()["detail"]
    # 无现行 OCR 题录的 DOI 不能校订
    r = client.post("/api/corrections",
                    json={"items": [{"doi": "10.1/none", "title": "t"}]})
    assert r.status_code == 400


# ---------- 三方合并矩阵（通过新 OCR 修订驱动） ----------

def reimport(client, doi, revision, title, abstract=""):
    return client.post("/api/records/import", files={"file": ("r.csv", make_csv([
        {"doi": doi, "revision": revision, "title": title,
         "abstract": abstract},
    ]))})


def test_only_ocr_changes_auto_merges_ocr_value(client):
    import_one(client, "10.1/m1", title="base T", abstract="base A")
    submit(client, [{"doi": "10.1/m1", "title": "base T",
                     "abstract": "human A"}])  # 人工只改摘要
    r = reimport(client, "10.1/m1", 2, title="new OCR T", abstract="base A")
    assert r.status_code == 201
    out = r.json()["correction_reprocessing"][0]
    assert out["status"] == "clean" and out["conflict_fields"] == []
    s = state_of(client, "10.1/m1")
    assert s["merged_title"] == "new OCR T" and s["title_source"] == "ocr"
    assert s["merged_abstract"] == "human A" and s["abstract_source"] == "human"


def test_only_human_changes_is_kept_on_new_identical_ocr(client):
    import_one(client, "10.1/m2", title="base T", abstract="base A")
    submit(client, [{"doi": "10.1/m2", "title": "human T",
                     "abstract": "base A"}])
    # 新 OCR 修订内容与基底相同（仅修订号增长）：人工题名继续保留
    r = reimport(client, "10.1/m2", 2, "base T", "base A")
    assert r.status_code == 201
    s = state_of(client, "10.1/m2")
    assert s["status"] == "clean"
    assert s["merged_title"] == "human T"
    assert s["title_source"] == "human"


def test_both_change_to_same_value_adopted(client):
    import_one(client, "10.1/m3", title="base T", abstract="")
    submit(client, [{"doi": "10.1/m3", "title": "same T", "abstract": ""}])
    r = reimport(client, "10.1/m3", 2, "same T", "")
    s = state_of(client, "10.1/m3")
    assert s["status"] == "clean"
    assert s["merged_title"] == "same T"
    assert s["title_source"] == "ocr"  # 同值直接采用，标记 OCR 侧
    assert r.json()["correction_reprocessing"][0]["conflict_fields"] == []


def test_divergent_changes_suspend_whole_record_keeping_three_versions(client):
    import_one(client, "10.1/c1", title="base T", abstract="base A")
    submit(client, [{"doi": "10.1/c1", "title": "human T",
                     "abstract": "human A"}])
    r = reimport(client, "10.1/c1", 2, "ocr T2", "base A")
    assert r.status_code == 201
    out = r.json()["correction_reprocessing"][0]
    assert out["status"] == "conflict"
    assert out["conflict_fields"] == ["title"]

    s = state_of(client, "10.1/c1")
    assert s["status"] == "conflict" and s["title_conflict"] is True
    assert s["abstract_conflict"] is False
    assert s["merged_title"] == ""  # 冲突字段不落合并值
    assert s["merged_abstract"] == "human A"  # 无冲突字段仍自动合并
    detail = s["conflict_detail"]["title"]
    assert detail == {"base": "base T", "ocr": "ocr T2", "human": "human T"}
    # 三方内容（含基底旧 OCR 行）完整保留
    assert s["base_ocr"]["title"] == "base T"
    assert s["live_ocr"]["title"] == "ocr T2"
    assert s["human"]["title"] == "human T"

    # 整条暂停供数：导出中不存在该 DOI
    assert "10.1/c1" not in export_map(client)
    counts = client.get("/api/status").json()["correction_counts"]
    assert counts["conflict_suspended"] == 1
    assert counts["supply_eligible"] == counts["live_records"] - 1


def test_abstract_conflict_also_suspends(client):
    import_one(client, "10.1/c2", title="T", abstract="base A")
    submit(client, [{"doi": "10.1/c2", "title": "T", "abstract": "human A"}])
    reimport(client, "10.1/c2", 2, "T", "ocr A2")
    s = state_of(client, "10.1/c2")
    assert s["status"] == "conflict"
    assert s["abstract_conflict"] is True
    assert "10.1/c2" not in export_map(client)


# ---------- 冲突裁决 ----------

def _seed_conflict(client, doi="10.1/rs"):
    import_one(client, doi, title="base T", abstract="base A")
    submit(client, [{"doi": doi, "title": "human T", "abstract": "human A"}])
    reimport(client, doi, 2, "ocr T2", "ocr A2")  # 两字段都冲突
    return state_of(client, doi)


def test_resolve_conflict_restores_supply(client):
    s = _seed_conflict(client)
    r = client.post(f"/api/corrections/{s['doi']}/resolve", json={
        "resolutions": {
            "title": {"source": "human"},
            "abstract": {"source": "ocr"},
        },
        "expected_ocr_record_id": s["live_ocr"]["id"],
        "expected_human_correction_id": s["human"]["id"],
    })
    assert r.status_code == 200, r.text
    s2 = state_of(client, s["doi"])
    assert s2["status"] == "clean"
    assert s2["merged_title"] == "human T"
    assert s2["merged_abstract"] == "ocr A2"
    assert s2["title_source"] == "manual"
    assert s2["resolution_ocr_record_id"] == s["live_ocr"]["id"]
    row = export_map(client)[s["doi"]]
    assert row["title"] == "human T" and row["abstract"] == "ocr A2"
    assert row["title_source"] == "manual"


def test_resolve_rejects_invalid_source_and_missing_field(client):
    s = _seed_conflict(client, "10.1/iv")
    url = f"/api/corrections/{s['doi']}/resolve"
    body = {"resolutions": {"title": {"source": "nope"},
                            "abstract": {"source": "ocr"}},
            "expected_ocr_record_id": s["live_ocr"]["id"],
            "expected_human_correction_id": s["human"]["id"]}
    assert client.post(url, json=body).status_code == 400
    body["resolutions"] = {"title": {"source": "base"}}  # 缺 abstract 裁决
    assert client.post(url, json=body).status_code == 400
    # 未解决前仍暂停供数
    assert s["doi"] not in export_map(client)


def test_stale_resolution_rejected_after_new_ocr_and_old_page_cannot_resume(client):
    s = _seed_conflict(client, "10.1/stale")
    stale_ocr = s["live_ocr"]["id"]
    stale_human = s["human"]["id"]
    # 又来一个 OCR 修订：重新处理，旧裁决绑定作废
    r = reimport(client, s["doi"], 3, "ocr T3", "ocr A3")
    assert r.json()["correction_reprocessing"][0]["status"] == "conflict"
    r = client.post(f"/api/corrections/{s['doi']}/resolve", json={
        "resolutions": {"title": {"source": "human"},
                        "abstract": {"source": "human"}},
        "expected_ocr_record_id": stale_ocr,
        "expected_human_correction_id": stale_human,
    })
    assert r.status_code == 409 and "变化" in r.json()["detail"]
    # 旧页面无法恢复供数
    assert s["doi"] not in export_map(client)
    # 用新绑定重新裁决成功
    s2 = state_of(client, s["doi"])
    r = client.post(f"/api/corrections/{s['doi']}/resolve", json={
        "resolutions": {"title": {"source": "base"},
                        "abstract": {"source": "human"}},
        "expected_ocr_record_id": s2["live_ocr"]["id"],
        "expected_human_correction_id": s2["human"]["id"],
    })
    assert r.status_code == 200
    assert s["doi"] in export_map(client)


def test_resaving_human_reprocesses_and_invalidates_old_resolution(client):
    s = _seed_conflict(client, "10.1/re")
    # 先裁决一次
    client.post(f"/api/corrections/{s['doi']}/resolve", json={
        "resolutions": {"title": {"source": "human"},
                        "abstract": {"source": "human"}},
        "expected_ocr_record_id": s["live_ocr"]["id"],
        "expected_human_correction_id": s["human"]["id"],
    })
    assert state_of(client, s["doi"])["status"] == "clean"
    # 重新保存人工版（基底变为当前 OCR）：自动合并，旧裁决被清空
    d = submit(client, [{"doi": s["doi"], "title": "human T v2",
                         "abstract": "human A v2"}])
    assert d["results"][0]["status"] == "clean"
    s2 = state_of(client, s["doi"])
    assert s2["human"]["id"] != s["human"]["id"]
    assert s2["base_ocr"]["id"] == s["live_ocr"]["id"]
    assert s2["resolution_detail"] is None
    assert s2["merged_title"] == "human T v2"
    # 旧人工版仍保留可审计
    from app.db import conn
    olds = conn().execute(
        "SELECT COUNT(*) n FROM human_corrections WHERE doi=? AND superseded=1",
        (s["doi"],),
    ).fetchone()["n"]
    assert olds == 1


def test_resolve_on_non_conflict_is_400(client):
    import_one(client, "10.1/nc", title="T", abstract="A")
    submit(client, [{"doi": "10.1/nc", "title": "T2", "abstract": "A"}])
    s = state_of(client, "10.1/nc")
    r = client.post("/api/corrections/10.1/nc/resolve", json={
        "resolutions": {"title": {"source": "human"}},
        "expected_ocr_record_id": s["live_ocr"]["id"],
        "expected_human_correction_id": s["human"]["id"],
    })
    assert r.status_code == 400


# ---------- 批次原子性 ----------

def test_submit_batch_is_all_or_nothing(client):
    import_one(client, "10.1/b1", title="T1", abstract="")
    import_one(client, "10.1/b2", title="T2", abstract="")
    # 第二项 DOI 不存在 -> 整批回滚，第一项不留任何校订痕迹
    r = client.post("/api/corrections", json={"items": [
        {"doi": "10.1/b1", "title": "edited T1", "abstract": ""},
        {"doi": "10.1/missing", "title": "x", "abstract": ""},
    ]})
    assert r.status_code == 400
    assert client.get("/api/corrections/10.1/b1").status_code == 404
    from app.db import conn
    assert conn().execute(
        "SELECT COUNT(*) n FROM human_corrections").fetchone()["n"] == 0
    # 拒收留痕
    batches = client.get("/api/status").json()["correction_batches"]
    assert batches[0]["status"] == "rejected" and batches[0]["kind"] == "submit"
    # 供数未受影响，b1 仍按 OCR 供数
    assert export_map(client)["10.1/b1"]["title"] == "T1"


def test_import_failure_rolls_back_correction_state_together(client):
    import_one(client, "10.1/roll", title="base T", abstract="")
    submit(client, [{"doi": "10.1/roll", "title": "human T", "abstract": ""}])
    # 同版异文触发整批拒收：重合并若已执行也必须随事务回滚
    r = reimport(client, "10.1/roll", 1, "different same revision", "")
    assert r.status_code == 400
    s = state_of(client, "10.1/roll")
    assert s["status"] == "clean"
    assert s["live_ocr"]["title"] == "base T"
    assert s["merged_title"] == "human T"


def test_audit_batch_records_reprocessing_outcomes(client):
    import_one(client, "10.1/au", title="base T", abstract="")
    submit(client, [{"doi": "10.1/au", "title": "human T", "abstract": ""}])
    reimport(client, "10.1/au", 2, "ocr T2", "")  # 冲突
    from app.db import conn
    detail = conn().execute(
        "SELECT detail FROM import_batches ORDER BY id DESC LIMIT 1"
    ).fetchone()["detail"]
    assert '"conflict"' in detail and "10.1/au" in detail


# ---------- 重启持久化 ----------

def test_restart_preserves_corrections_conflicts_and_eligibility(client):
    s = _seed_conflict(client, "10.1/persist")
    import_one(client, "10.1/plain", title="plain OCR", abstract="")
    before = export_rows(client)

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as client2:
        # 冲突依旧暂停，三方内容仍在
        r = client2.get("/api/corrections/10.1/persist")
        s2 = r.json()
        assert s2["status"] == "conflict"
        assert s2["conflict_detail"]["title"]["human"] == "human T"
        assert s2["human"]["id"] == s["human"]["id"]
        assert "10.1/persist" not in {
            x["doi"] for x in csv.DictReader(io.StringIO(
                client2.get("/api/export/supply.csv").text))}
        # 可供数集合与重启前完全一致
        after = list(csv.DictReader(io.StringIO(
            client2.get("/api/export/supply.csv").text)))
        assert [(r_["doi"], r_["title"], r_["abstract"]) for r_ in before] == \
               [(r_["doi"], r_["title"], r_["abstract"]) for r_ in after]
        # 重启后裁决仍可成功并恢复供数
        rr = client2.post("/api/corrections/10.1/persist/resolve", json={
            "resolutions": {"title": {"source": "human"},
                            "abstract": {"source": "human"}},
            "expected_ocr_record_id": s2["live_ocr"]["id"],
            "expected_human_correction_id": s2["human"]["id"],
        })
        assert rr.status_code == 200


# ---------- 与原检索/词典能力并存 ----------

def test_search_still_returns_raw_ocr_records_including_corrected(client):
    import_one(client, "10.1/sr", title="Teh quantum study",
               abstract="machne data")
    submit(client, [{"doi": "10.1/sr", "title": "The quantum study",
                     "abstract": "machine data"}])
    # 原始题录与检索照常：检索仍基于 OCR 原文 + 词典归一，返回原始题名
    r = client.get("/api/search", params={"q": "teh"})
    assert r.json()["count"] == 1
    assert r.json()["results"][0]["title"] == "Teh quantum study"
    # 供数 CSV 用的是人工合并版
    assert export_map(client)["10.1/sr"]["title"] == "The quantum study"


def test_dictionary_rebuild_still_works_with_corrections(client):
    from conftest import dict_csv
    import_one(client, "10.1/d", title="teh optics paper", abstract="")
    submit(client, [{"doi": "10.1/d", "title": "the optics paper",
                     "abstract": ""}])
    r = client.post("/api/dictionaries",
                    files={"file": ("d.csv", dict_csv([("teh", "the")]))})
    assert r.status_code == 202
    wait_until(lambda: not client.get("/api/status").json()["rebuild_in_progress"])
    assert client.get("/api/search", params={"q": "the"}).json()["count"] == 1
    # 校订状态不受换词典影响
    assert state_of(client, "10.1/d")["status"] == "clean"


def test_export_snapshot_consistency_single_statement(client):
    # 多条混合：未校订 + clean + conflict，导出只含前两类且列齐全
    import_one(client, "10.1/e1", title="ocr1", abstract="a1")
    import_one(client, "10.1/e2", title="base2", abstract="a2")
    import_one(client, "10.1/e3", title="base3", abstract="a3")
    submit(client, [{"doi": "10.1/e2", "title": "human2", "abstract": "a2"}])
    submit(client, [{"doi": "10.1/e3", "title": "human3", "abstract": "a3"}])
    reimport(client, "10.1/e3", 2, "ocr3v2", "a3")  # 制造冲突
    rows = export_rows(client)
    dois = {r["doi"] for r in rows}
    assert dois == {"10.1/e1", "10.1/e2"}
    header = list(rows[0].keys())
    for col in ("doi", "revision", "title", "abstract", "title_source",
                "abstract_source", "ocr_base_record_id",
                "ocr_live_record_id", "human_correction_id"):
        assert col in header

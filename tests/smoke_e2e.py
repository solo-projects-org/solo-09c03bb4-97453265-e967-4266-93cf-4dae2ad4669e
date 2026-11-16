"""真实 uvicorn 子进程端到端冒烟：含崩溃重启恢复与人工校订（非 TestClient）。"""
import csv
import io
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

ROOT = Path("/workspace")
DB = "/tmp/smoke_e2e/e2e.db"
PORT = 8125
BASE = f"http://127.0.0.1:{PORT}"
PY = str(ROOT / ".venv/bin/python")

os.makedirs("/tmp/smoke_e2e", exist_ok=True)
for suffix in ("", "-wal", "-shm"):
    p = Path(DB + suffix)
    if p.exists():
        p.unlink()


def server(log: str) -> subprocess.Popen:
    env = dict(os.environ, DB_PATH=DB)
    f = open(log, "wb")
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(PORT), "--workers", "1"],
        cwd=ROOT, env=env, stdout=f, stderr=subprocess.STDOUT,
    )


def wait_health(proc, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            raise RuntimeError("server exited early")
        try:
            with urllib.request.urlopen(f"{BASE}/health", timeout=1) as r:
                if r.status == 200:
                    return
        except Exception:
            time.sleep(0.2)
    raise RuntimeError("server not healthy")


def http(method, path, data=None, ctype=None):
    req = urllib.request.Request(BASE + path, data=data, method=method)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def multipart(field, filename, content):
    boundary = "----smokeboundary"
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; "
        f"filename=\"{filename}\"\r\nContent-Type: text/csv\r\n\r\n"
    ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def check(name, cond, extra=""):
    print(("PASS" if cond else "FAIL"), name, extra)
    if not cond:
        raise SystemExit(1)


records = (
    b"doi,revision,title,abstract\n"
    b"10.1/a,1,Teh Quantum Optics,studis of photons\n"
    b"10.1/b,1,Machne Lerning Survey,neural netwark\n"
    b"10.1/c,1,Catalysis Tehniques,cheminal kinetics\n"
)
dictionary = (
    b"wrong,correct\nteh,the\nmachne,machine\nlerning,learning\n"
    b"studis,studies\nnetwark,network\ncheminal,chemical\n"
)

# --- 进程 1：正常业务 ---
p = server("/tmp/smoke_e2e/a.log")
wait_health(p)
try:
    body, ctype = multipart("file", "r.csv", records)
    code, d = http("POST", "/api/records/import", body, ctype)
    check("初始导入 3 条", code == 201 and d["accepted"] == 3, str(d))

    code, d = http("GET", "/api/search?q=" + urllib.parse.quote("the machine"))
    check("无词典漏检", code == 200 and d["count"] == 0)

    body, ctype = multipart("file", "d.csv", dictionary)
    code, d = http("POST", "/api/dictionaries", body, ctype)
    check("词典上传受理", code == 202 and d["status"] == "building")
    deadline = time.time() + 15
    while time.time() < deadline:
        _, st = http("GET", "/api/status")
        if not st["rebuild_in_progress"]:
            break
        time.sleep(0.2)
    check("第一版发布", st["active_dict_version_id"] >= 1)
    code, d = http("GET", "/api/search?q=" + urllib.parse.quote("machine learning"))
    check("纠错后命中 machine learning", d["count"] == 1, str(d.get("results")))
    code, d = http("GET", "/api/search?q=" + urllib.parse.quote("TEH"))
    check("查询词归一(大写错词)", d["count"] == 1)
    # 整词边界：Tehniques 不应被替换
    code, d = http("GET", "/api/search?q=" + urllib.parse.quote("tehniques"))
    check("整词匹配不破坏 Tehniques", d["count"] == 1)
finally:
    p.send_signal(signal.SIGTERM)
    p.wait(timeout=10)

# --- 磁盘上制造“构建中途被 kill -9”的现场 ---
con = sqlite3.connect(DB, isolation_level=None)
con.execute("BEGIN IMMEDIATE")
con.execute("CREATE VIRTUAL TABLE fts_v3 USING fts5("
            "title_norm,abstract_norm,tokenize='unicode61')")
con.execute("INSERT INTO fts_v3(rowid,title_norm) VALUES(1,'partial halfbuilt')")
con.execute("INSERT INTO dict_versions(filename,status,entry_count,base_fts,"
            "fts_table) VALUES('crash.csv','building',1,'fts_v2','fts_v3')")
vid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
con.execute("INSERT INTO dict_entries VALUES(?,?,?)", (vid, "wip", "wop"))
con.execute("INSERT INTO rebuild_jobs(dict_version_id,status,last_id) "
            "VALUES(?, 'running', 1)", (vid,))
con.execute("CREATE VIRTUAL TABLE fts_v88 USING fts5(x)")  # 孤儿表
con.execute("COMMIT")
con.close()
print("已制造崩溃现场，半成品版本 id =", vid)

# --- 进程 2：重启恢复 ---
p2 = server("/tmp/smoke_e2e/b.log")
wait_health(p2)
try:
    _, st = http("GET", "/api/status")
    check("旧索引继续服务", st["active_dict_version_id"] == 1
          and st["active_fts"] == "fts_v2")
    check("中断版本标记 failed",
          any(v["id"] == vid and v["status"] == "failed"
              for v in st["dict_versions"]))
    check("孤儿 FTS 被清理", "fts_v88" in st["startup_recovery"]["orphan_fts"])
    check("无活动重建", st["rebuild_in_progress"] is False)
    code, d = http("GET", "/api/search?q=machine")
    check("旧词典检索可用", d["count"] == 1)
    # 半成品表绝不暴露：active 仍是 fts_v2
    check("半成品未发布", st["active_fts"] != "fts_v3")

    # 重试 -> 成功发布，且数据完整
    code, d = http("POST", f"/api/dictionaries/{vid}/retry")
    check("重试受理", code == 200)
    deadline = time.time() + 15
    while time.time() < deadline:
        _, st2 = http("GET", "/api/status")
        if not st2["rebuild_in_progress"]:
            break
        time.sleep(0.2)
    check("重试后发布", st2["active_dict_version_id"] == vid
          and st2["active_fts"] == "fts_v3")
    code, d = http("GET", "/api/search?q=quantum")
    check("新索引数据完整(3 条现行记录可检)", d["count"] == 1)
    con = sqlite3.connect(DB)
    rowids = sorted(r[0] for r in con.execute(
        "SELECT rowid FROM fts_v3"))
    check("新 FTS 覆盖全部现行记录", rowids == [1, 2, 3], str(rowids))
    import re
    masters = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name GLOB 'fts_v[0-9]*'")
        if re.match(r"^fts_v\d+$", r[0])]
    check("旧 FTS 已删除、无孤儿", masters == ["fts_v3"], str(masters))
    con.close()

    # --- 人工校订：提交、OCR 自动合并、冲突暂停、裁决恢复、供数 CSV ---
    def jpost(path, payload):
        body = json.dumps(payload).encode()
        return http("POST", path, body, "application/json")

    body, ctype = multipart("file", "corr.csv",
                            b"doi,revision,title,abstract\n"
                            b"10.9/a,1,base title A,base abstract A\n"
                            b"10.9/b,1,base title B,base abstract B\n"
                            b"10.9/c,1,ocr only title,ocr only abs\n")
    code, d = http("POST", "/api/records/import", body, ctype)
    check("校订用题录导入", code == 201, str(d))

    code, d = jpost("/api/corrections", {"items": [
        {"doi": "10.9/a", "title": "base title A",
         "abstract": "human abstract A"},
        {"doi": "10.9/b", "title": "human title B",
         "abstract": "human abstract B"},
    ]})
    check("人工校订整批提交", code == 201 and d["auto_merged"] == 2, str(d))

    # 新 OCR 修订：a 题名仅 OCR 改、摘要仅人工改 -> 两字段都自动合并；
    # b 两字段都分歧 -> 冲突整条暂停。
    body, ctype = multipart("file", "rev2.csv",
                            b"doi,revision,title,abstract\n"
                            b"10.9/a,2,new ocr title A,base abstract A\n"
                            b"10.9/b,2,new ocr title B,new ocr abstract B\n"
                            b"10.9/c,2,ocr only title v2,ocr only abs v2\n")
    code, d = http("POST", "/api/records/import", body, ctype)
    check("带重合并的 OCR 批次接纳", code == 201, str(d))
    rep = {x["doi"]: x for x in d["correction_reprocessing"]}
    check("a 自动合并 / b 冲突",
          rep["10.9/a"]["status"] == "clean"
          and rep["10.9/b"]["status"] == "conflict"
          and rep["10.9/b"]["conflict_fields"] == ["title", "abstract"],
          str(rep))

    code, st = http("GET", "/api/corrections/10.9/b")
    check("冲突三方内容保留", code == 200 and st["status"] == "conflict"
          and st["conflict_detail"]["title"] == {
              "base": "base title B", "ocr": "new ocr title B",
              "human": "human title B"}, str(st.get("conflict_detail")))

    # 旧版本绑定的裁决页面 -> 409 路径先构造“新鲜”绑定再验证过期场景
    code, fresh = http("GET", "/api/corrections/10.9/b")
    code, d = jpost("/api/corrections/10.9/b/resolve", {
        "resolutions": {"title": {"source": "human"},
                        "abstract": {"source": "ocr"}},
        "expected_ocr_record_id": fresh["live_ocr"]["id"] - 999,
        "expected_human_correction_id": fresh["human"]["id"],
    })
    check("过期页面裁决被拒(409)", code == 409, str(d))

    code, d = jpost("/api/corrections/10.9/b/resolve", {
        "resolutions": {"title": {"source": "human"},
                        "abstract": {"source": "ocr"}},
        "expected_ocr_record_id": fresh["live_ocr"]["id"],
        "expected_human_correction_id": fresh["human"]["id"],
    })
    check("新鲜页面裁决成功恢复供数", code == 200, str(d))

    with urllib.request.urlopen(f"{BASE}/api/export/supply.csv",
                                timeout=10) as r:
        check("供数 CSV 可下载", r.status == 200
              and r.headers["Content-Type"].startswith("text/csv"))
        csv_text = r.read().decode()
    supply = {row["doi"]: row for row in csv.DictReader(io.StringIO(csv_text))}
    check("供数 CSV 含全部现行题录（冲突已裁决）",
          {f"10.9/{x}" for x in "abc"} <= set(supply), str(supply.keys()))
    check("a 题名取 OCR 侧自动合并值",
          supply["10.9/a"]["title"] == "new ocr title A"
          and supply["10.9/a"]["title_source"] == "ocr",
          supply["10.9/a"])
    check("b 题名取人工裁决值、摘要取 OCR 裁决值",
          supply["10.9/b"]["title"] == "human title B"
          and supply["10.9/b"]["abstract"] == "new ocr abstract B"
          and supply["10.9/b"]["title_source"] == "manual",
          supply["10.9/b"])
    check("未校订 c 沿用最新 OCR",
          supply["10.9/c"]["title"] == "ocr only title v2"
          and supply["10.9/c"]["human_correction_id"] == "",
          supply["10.9/c"])

    # 再制造一个未裁决冲突，验证重启后冲突持久、供数仍排除
    code, d = jpost("/api/corrections", {"items": [
        {"doi": "10.9/c", "title": "human title C",
         "abstract": "ocr only abs v2"}]})
    check("c 人工校订提交", code == 201, str(d))
    body, ctype = multipart("file", "rev3.csv",
                            b"doi,revision,title,abstract\n"
                            b"10.9/c,3,divergent ocr C,ocr only abs v2\n")
    code, d = http("POST", "/api/records/import", body, ctype)
    check("c 冲突批次接纳", code == 201
          and d["correction_reprocessing"][0]["status"] == "conflict", str(d))
    before_csv = csv_text
finally:
    p2.send_signal(signal.SIGTERM)
    p2.wait(timeout=10)

# --- 进程 3：重启后校订/冲突/供数资格持久化 ---
p3 = server("/tmp/smoke_e2e/c.log")
wait_health(p3)
try:
    code, st = http("GET", "/api/corrections/10.9/c")
    check("重启后冲突保留", code == 200 and st["status"] == "conflict"
          and st["conflict_detail"]["title"]["human"] == "human title C",
          str(st.get("conflict_detail")))
    code, st = http("GET", "/api/corrections/10.9/b")
    check("重启后已裁决状态保留", code == 200 and st["status"] == "clean"
          and st["merged_title"] == "human title B", str(st))
    with urllib.request.urlopen(f"{BASE}/api/export/supply.csv",
                                timeout=10) as r:
        after_csv = r.read().decode()
    supply3 = {row["doi"]: row for row in csv.DictReader(
        io.StringIO(after_csv))}
    check("重启后冲突题录仍不供数", "10.9/c" not in supply3, str(supply3.keys()))
    check("重启后其余供数内容不变",
          {k: (v["title"], v["abstract"]) for k, v in supply3.items()}
          == {k: (v["title"], v["abstract"])
              for k, v in {row["doi"]: row for row in
                           csv.DictReader(io.StringIO(before_csv))}.items()
              if k != "10.9/c"})
    # 重启后仍可裁决并恢复供数
    code, fresh = http("GET", "/api/corrections/10.9/c")
    code, d = jpost("/api/corrections/10.9/c/resolve", {
        "resolutions": {"title": {"source": "human"}},
        "expected_ocr_record_id": fresh["live_ocr"]["id"],
        "expected_human_correction_id": fresh["human"]["id"],
    })
    check("重启后裁决成功", code == 200, str(d))
    with urllib.request.urlopen(f"{BASE}/api/export/supply.csv",
                                timeout=10) as r:
        final_csv = r.read().decode()
    check("裁决后恢复供数", "10.9/c" in
          {row["doi"] for row in csv.DictReader(io.StringIO(final_csv))})
finally:
    p3.send_signal(signal.SIGTERM)
    p3.wait(timeout=10)

print("\n全部端到端冒烟检查通过（含人工校订三方合并与重启持久化）")

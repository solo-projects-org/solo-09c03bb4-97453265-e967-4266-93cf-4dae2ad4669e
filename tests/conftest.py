import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import rebuild as rebuild_mod  # noqa: E402
from app import state  # noqa: E402
from app.db import set_path  # noqa: E402

# 小批量，保证重建分多批，便于注入检查点。
rebuild_mod.BATCH_SIZE = 2


@pytest.fixture()
def client(tmp_path):
    db = tmp_path / "test.db"
    set_path(str(db))
    state.set_mapping({})
    state.fail_after_id = None
    state.checkpoint_id = None
    state.checkpoint_event = None
    state.hold_event = None

    from fastapi.testclient import TestClient
    from app.main import app

    with TestClient(app) as c:
        yield c


def make_csv(rows: list[dict], columns=None) -> bytes:
    import csv
    import io

    columns = columns or ["doi", "revision", "title", "abstract"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=columns)
    w.writeheader()
    for r in rows:
        w.writerow({k: r.get(k, "") for k in columns})
    return buf.getvalue().encode("utf-8")


def dict_csv(pairs: list[tuple[str, str]]) -> bytes:
    import io

    buf = io.StringIO()
    buf.write("wrong,correct\n")
    for w, k in pairs:
        buf.write(f"{w},{k}\n")
    return buf.getvalue().encode("utf-8")


def wait_until(predicate, timeout=15.0, interval=0.05):
    import time

    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    raise AssertionError("等待条件超时")

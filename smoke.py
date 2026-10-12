"""Sanity check the worker runs after every agent change. Exit code 0 means OK.

It applies all migrations to a *copy* of the real database and exercises the main
pages through FastAPI's TestClient. Extend it when you add important features.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

tmp_db = Path(tempfile.mkdtemp()) / "tickets.db"
real_db = ROOT / "data" / "tickets.db"
if real_db.exists():
    src, dst = sqlite3.connect(real_db), sqlite3.connect(tmp_db)
    src.backup(dst)
    src.close()
    dst.close()
os.environ["TIX_DB"] = str(tmp_db)

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect  # noqa: E402
from app.main import app  # noqa: E402


def check(resp, what):
    assert resp.status_code == 200, f"{what}: HTTP {resp.status_code}\n{resp.text[:2000]}"


with TestClient(app) as c:  # entering runs the lifespan, i.e. the migrations
    check(c.get("/healthz"), "GET /healthz")
    check(c.get("/"), "GET /")
    r = c.post(
        "/tickets",
        data={"title": "smoke test", "description": "created by smoke.py", "prio": "3"},
    )
    check(r, "POST /tickets")
    conn = connect()
    ids = [row[0] for row in conn.execute("SELECT id FROM tickets ORDER BY id DESC LIMIT 20")]
    conn.close()
    for tid in ids:
        check(c.get(f"/tickets/{tid}"), f"GET /tickets/{tid}")
        check(c.get(f"/api/tickets/{tid}"), f"GET /api/tickets/{tid}")

    conn = connect()
    prio = conn.execute("SELECT prio FROM tickets WHERE id = ?", (ids[0],)).fetchone()[0]
    conn.close()
    assert prio == 3, f"prio should be stored as 3, got {prio}"

print(f"smoke OK ({len(ids)} ticket pages checked)")

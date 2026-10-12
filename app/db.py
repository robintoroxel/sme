"""SQLite access and file-based migrations.

Schema changes are made by adding a new file to app/migrations/ named
NNN_description.sql. Migrations are applied in filename order, once each,
at web-server startup and by the worker before every run.
"""
import os
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"


def db_path() -> Path:
    return Path(os.environ.get("TIX_DB", ROOT / "data" / "tickets.db"))


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = Path(path or db_path())
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        " name TEXT PRIMARY KEY,"
        " applied_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.commit()
    done = {r[0] for r in conn.execute("SELECT name FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.name in done:
            continue
        sql = f.read_text(encoding="utf-8")
        name = f.name.replace("'", "''")
        try:
            conn.executescript(
                f"BEGIN;\n{sql}\n;INSERT INTO schema_migrations(name) VALUES('{name}');\nCOMMIT;"
            )
        except Exception:
            conn.rollback()
            raise
        applied.append(f.name)
    return applied

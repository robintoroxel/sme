"""Web UI for the ticket system."""
import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import gitops, runs
from .db import ROOT, connect, migrate, orphan_migrations

APP_DIR = Path(__file__).resolve().parent
LOG_DIR = ROOT / "data" / "logs"
MODELS = ["deepseek/deepseek-v4-pro", "deepseek/deepseek-flash"]
STATUSES = ["queued", "running", "review", "done", "failed", "cancelled", "rejected", "reverted"]
PRIO_LABELS = {1: "low", 2: "medium", 3: "high"}
DEFAULT_PRIO = 2


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn = connect()
    migrate(conn)
    conn.close()
    yield


app = FastAPI(title="tix", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=APP_DIR / "static"), name="static")


def fmt_duration(seconds) -> str:
    if seconds is None:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


templates = Jinja2Templates(directory=APP_DIR / "templates")
templates.env.filters["duration"] = fmt_duration


def db():
    return connect()


def see_other(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def get_ticket(conn, ticket_id: int):
    t = conn.execute("SELECT * FROM tickets WHERE id = ?", (ticket_id,)).fetchone()
    if not t:
        raise HTTPException(404, "Ticket not found")
    return t


def worker_status() -> dict:
    try:
        return json.loads((ROOT / "data" / "worker.json").read_text())
    except (OSError, ValueError):
        return {}


def read_log(run_id: int) -> str:
    p = LOG_DIR / f"run-{run_id}.log"
    return p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, status: str | None = None):
    conn = db()
    q = (
        "SELECT t.*,"
        " (SELECT SUM(r.duration_seconds) FROM runs r"
        " WHERE r.ticket_id = t.id AND r.status = 'done') AS impl_seconds"
        " FROM tickets t"
    )
    args: tuple = ()
    if status in STATUSES:
        q += " WHERE t.status = ?"
        args = (status,)
    tickets = conn.execute(q + " ORDER BY t.id DESC", args).fetchall()
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM tickets GROUP BY status").fetchall())
    orphans = orphan_migrations(conn)
    conn.close()
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "tickets": tickets,
            "counts": counts,
            "status": status,
            "statuses": STATUSES,
            "models": MODELS,
            "prios": PRIO_LABELS,
            "default_prio": DEFAULT_PRIO,
            "orphans": orphans,
            "worker": worker_status(),
        },
    )


@app.post("/tickets")
def create_ticket(
    title: str = Form(...),
    description: str = Form(""),
    model: str = Form(MODELS[0]),
    prio: int = Form(DEFAULT_PRIO),
):
    title = title.strip()
    if not title:
        raise HTTPException(400, "Title is required")
    if model not in MODELS:
        model = MODELS[0]
    if prio not in PRIO_LABELS:
        prio = DEFAULT_PRIO
    conn = db()
    cur = conn.execute(
        "INSERT INTO tickets (title, description, model, prio) VALUES (?, ?, ?, ?)",
        (title, description.strip(), model, prio),
    )
    tid = cur.lastrowid
    prompt = f"Ticket #{tid}: {title}\n\n{description.strip()}".strip()
    conn.execute("INSERT INTO runs (ticket_id, prompt) VALUES (?, ?)", (tid, prompt))
    conn.commit()
    conn.close()
    return see_other(f"/tickets/{tid}")


@app.get("/tickets/{ticket_id}", response_class=HTMLResponse)
def ticket_detail(request: Request, ticket_id: int, error: str | None = None):
    conn = db()
    t = get_ticket(conn, ticket_id)
    runs = conn.execute("SELECT * FROM runs WHERE ticket_id = ? ORDER BY id", (ticket_id,)).fetchall()
    conn.close()
    logs = {r["id"]: read_log(r["id"]) for r in runs}
    active = any(r["status"] in ("queued", "running") for r in runs)
    return templates.TemplateResponse(
        request,
        "ticket.html",
        {"t": t, "runs": runs, "logs": logs, "active": active, "error": error, "prio_labels": PRIO_LABELS},
    )


@app.post("/tickets/{ticket_id}/followup")
def followup(ticket_id: int, body: str = Form(...)):
    body = body.strip()
    if not body:
        return see_other(f"/tickets/{ticket_id}")
    conn = db()
    get_ticket(conn, ticket_id)
    conn.execute("INSERT INTO runs (ticket_id, prompt) VALUES (?, ?)", (ticket_id, body))
    runs.refresh_ticket_status(conn, ticket_id)
    conn.commit()
    conn.close()
    return see_other(f"/tickets/{ticket_id}")


@app.post("/tickets/{ticket_id}/cancel")
def cancel(ticket_id: int):
    conn = db()
    get_ticket(conn, ticket_id)
    conn.execute(
        "UPDATE runs SET status = 'cancelled', finished_at = datetime('now')"
        " WHERE ticket_id = ? AND status = 'queued'",
        (ticket_id,),
    )
    conn.execute(
        "UPDATE runs SET cancel_requested = 1 WHERE ticket_id = ? AND status = 'running'", (ticket_id,)
    )
    runs.refresh_ticket_status(conn, ticket_id)
    conn.commit()
    conn.close()
    return see_other(f"/tickets/{ticket_id}")


@app.post("/tickets/{ticket_id}/revert")
def revert(ticket_id: int):
    conn = db()
    get_ticket(conn, ticket_id)
    if conn.execute("SELECT 1 FROM runs WHERE status IN ('queued', 'running')").fetchone():
        conn.close()
        return see_other(f"/tickets/{ticket_id}?error=Wait+until+no+agent+run+is+queued+or+running.")
    shas = [
        r["commit_sha"]
        for r in conn.execute(
            "SELECT commit_sha FROM runs WHERE ticket_id = ? AND commit_sha IS NOT NULL ORDER BY id DESC",
            (ticket_id,),
        )
    ]
    ok, msg = gitops.revert(shas)
    if ok:
        conn.execute(
            "UPDATE tickets SET status = 'reverted', updated_at = datetime('now') WHERE id = ?", (ticket_id,)
        )
        conn.commit()
    conn.close()
    if not ok:
        return see_other(f"/tickets/{ticket_id}?error=Revert+failed:+{msg[:300]}")
    return see_other(f"/tickets/{ticket_id}")


def get_run(conn, run_id: int):
    r = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if not r:
        raise HTTPException(404, "Run not found")
    return r


@app.post("/runs/{run_id}/approve")
def approve(run_id: int):
    conn = db()
    r = get_run(conn, run_id)
    if r["status"] != "review":
        conn.close()
        return see_other(f"/tickets/{r['ticket_id']}")
    ok, msg = runs.merge_run(conn, run_id)
    conn.close()
    if not ok:
        return see_other(f"/tickets/{r['ticket_id']}?error=Merge+failed:+{msg[:300]}")
    return see_other(f"/tickets/{r['ticket_id']}")


@app.post("/runs/{run_id}/reject")
def reject(run_id: int):
    conn = db()
    r = get_run(conn, run_id)
    if r["status"] == "review":
        runs.reject_run(conn, run_id)
    conn.close()
    return see_other(f"/tickets/{r['ticket_id']}")


@app.get("/runs/{run_id}/log", response_class=PlainTextResponse)
def run_log(run_id: int):
    return read_log(run_id)


@app.get("/runs/{run_id}/diff", response_class=PlainTextResponse)
def run_diff(run_id: int):
    conn = db()
    r = get_run(conn, run_id)
    conn.close()
    if r["commit_sha"]:
        return gitops.show(r["commit_sha"])
    if r["branch"] and gitops.branch_exists(r["branch"]):
        return gitops.branch_diff(r["branch"])
    raise HTTPException(404, "No changes recorded for this run")


@app.get("/api/tickets/{ticket_id}")
def ticket_json(ticket_id: int):
    conn = db()
    t = get_ticket(conn, ticket_id)
    runs = conn.execute(
        "SELECT id, status, commit_sha, summary, error FROM runs WHERE ticket_id = ? ORDER BY id", (ticket_id,)
    ).fetchall()
    conn.close()
    return JSONResponse({"status": t["status"], "runs": [dict(r) for r in runs]})

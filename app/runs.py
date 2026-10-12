"""Run and ticket state transitions shared by the web app and the worker.

Run statuses: queued, running, review (waiting for approval), done (merged or no
changes), failed, cancelled, rejected, superseded (replaced by a later follow-up run).
"""
import re
import sys
from pathlib import Path, PurePosixPath

from . import gitops

# Changes to these files always need human approval: they make up the agent's own
# guardrails (worker, checks, rules, permissions, git/DB plumbing, dependencies).
PROTECTED = (
    "worker.py",
    "smoke.py",
    "run.sh",
    "AGENTS.md",
    "opencode.json",
    "app/__init__.py",  # runs on every `from app import ...` in the worker
    "app/db.py",
    "app/gitops.py",
    "app/runs.py",
    "pyproject.toml",  # run.sh runs `uv sync`, so dependency changes install unreviewed
    "uv.lock",
    ".gitignore",  # e.g. dropping `.env` would get secrets auto-committed
    ".gitattributes",
)
# Top-level modules shadow the standard library for worker.py, smoke.py and the checks
# (the script dir / cwd comes first on sys.path), e.g. a `time.py` would run inside the worker.
SHADOWING = set(sys.stdlib_module_names) | {"sitecustomize", "usercustomize"}

MIGRATION_RE = re.compile(r"^(\d+)_[\w-]+\.sql$")
ATTACH_RE = re.compile(r"\b(ATTACH|DETACH)\b", re.IGNORECASE)  # would reach other database files


def protected_changes(files: list[str]) -> list[str]:
    out = []
    for f in files:
        p = PurePosixPath(f)
        if (
            f in PROTECTED
            or (len(p.parts) == 1 and p.suffix == ".py")  # any top-level module
            or (len(p.parts) > 1 and p.parts[0] in SHADOWING)  # a top-level package like json/
        ):
            out.append(f)
    return out


def migration_problems(work_dir: Path, live_head: str) -> list[str]:
    """Migrations in the live HEAD are already applied to the live DB: they must stay unchanged,
    and new ones must be numbered after the highest of them."""
    live = gitops.git("ls-tree", "--name-only", live_head, "app/migrations/", cwd=work_dir).stdout.split()
    live = [PurePosixPath(f).name for f in live if f.endswith(".sql")]
    problems = []
    for name in live:
        path = f"app/migrations/{name}"
        if gitops.git("diff", "--quiet", live_head, "--", path, cwd=work_dir, check=False).returncode:
            problems.append(f"{path} was modified or deleted; existing migrations must not change.")
    top = max((int(m.group(1)) for n in live if (m := MIGRATION_RE.match(n))), default=0)
    seen: dict[int, str] = {}
    for f in sorted((work_dir / "app" / "migrations").glob("*.sql")):
        if f.name in live:
            continue
        m = MIGRATION_RE.match(f.name)
        if not m:
            problems.append(f"app/migrations/{f.name}: name must look like NNN_description.sql.")
            continue
        n = int(m.group(1))
        if n <= top:
            problems.append(f"app/migrations/{f.name}: number must be higher than {top:03d}.")
        if ATTACH_RE.search(f.read_text(encoding="utf-8", errors="replace")):
            problems.append(f"app/migrations/{f.name}: ATTACH/DETACH is not allowed in migrations.")
        if n in seen:
            problems.append(f"app/migrations/{f.name}: number {n:03d} is also used by {seen[n]}.")
        seen[n] = f.name
    return problems


def refresh_ticket_status(conn, ticket_id: int) -> None:
    statuses = [r[0] for r in conn.execute("SELECT status FROM runs WHERE ticket_id = ? ORDER BY id", (ticket_id,))]
    if not statuses:
        return
    for s in ("running", "queued", "review"):
        if s in statuses:
            status = s
            break
    else:
        status = statuses[-1]
    conn.execute("UPDATE tickets SET status = ?, updated_at = datetime('now') WHERE id = ?", (status, ticket_id))


def supersede_older(conn, run) -> None:
    """A newer run of the same ticket builds on older unmerged branches; retire those."""
    older = conn.execute(
        "SELECT id, branch FROM runs WHERE ticket_id = ? AND status = 'review' AND id < ?",
        (run["ticket_id"], run["id"]),
    ).fetchall()
    for r in older:
        if r["branch"]:
            gitops.delete_branch(r["branch"])
        conn.execute("UPDATE runs SET status = 'superseded' WHERE id = ?", (r["id"],))


def merge_run(conn, run_id: int) -> tuple[bool, str]:
    """Squash-merge a run's branch into the live checkout (it goes live via auto-reload)."""
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    t = conn.execute("SELECT * FROM tickets WHERE id = ?", (run["ticket_id"],)).fetchone()
    if not run["branch"] or not gitops.branch_exists(run["branch"]):
        return False, "The run's branch no longer exists."
    message = f"#{t['id']} {t['title']}\n\n{(run['summary'] or '')[:3000]}\n\ntix run {run_id}"
    sha, err = gitops.merge_squash(run["branch"], message)
    if err:
        conn.execute("UPDATE runs SET review_reason = ? WHERE id = ?", (f"Merge failed: {err[:1000]}", run_id))
        conn.commit()
        return False, err
    conn.execute(
        "UPDATE runs SET status = 'done', commit_sha = ?, review_reason = NULL WHERE id = ?", (sha, run_id)
    )
    supersede_older(conn, run)
    refresh_ticket_status(conn, t["id"])
    conn.commit()
    return True, ""


def reject_run(conn, run_id: int) -> None:
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    if run["branch"]:
        gitops.delete_branch(run["branch"])
    conn.execute("UPDATE runs SET status = 'rejected' WHERE id = ?", (run_id,))
    refresh_ticket_status(conn, run["ticket_id"])
    conn.commit()

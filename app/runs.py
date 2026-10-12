"""Run and ticket state transitions shared by the web app and the worker.

Run statuses: queued, running, review (waiting for approval), done (merged or no
changes), failed, cancelled, rejected, superseded (replaced by a later follow-up run).
"""
from . import gitops

# Changes to these files always need human approval: they make up the agent's own
# guardrails (worker, checks, rules, permissions, git/DB plumbing).
PROTECTED = (
    "worker.py",
    "smoke.py",
    "run.sh",
    "AGENTS.md",
    "opencode.json",
    "app/db.py",
    "app/gitops.py",
    "app/runs.py",
)


def protected_changes(files: list[str]) -> list[str]:
    return [f for f in files if f in PROTECTED]


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

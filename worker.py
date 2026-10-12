"""Agent worker: picks queued runs and lets opencode implement them.

The agent never touches the live checkout. For each run:
  1. commit any manual (uncommitted) changes in the live checkout
  2. create a staging clone (TIX_WORK_DIR) on branch tix/run-N with a copy of the live DB
     (a follow-up on a ticket that is awaiting review builds on that review branch)
  3. run `opencode run` there (follow-ups continue the session)
  4. run the checks (compile, import, smoke.py); on failure let the agent fix it, FIX_ATTEMPTS times
  5. commit on the branch; then either squash-merge into the live checkout (it goes live via
     auto-reload) or, if protected files changed / TIX_AUTO_MERGE=never, wait for approval in the UI
The worker re-executes itself after every run and whenever the live HEAD moves, so merged
changes to its own code take effect. run.sh falls back to the last known-good worker
(git tag tix-worker-good) if the current one keeps crashing.
"""
import json
import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

from app import gitops, runs
from app.db import ROOT, connect, copy_db, db_path, migrate

LOG_DIR = ROOT / "data" / "logs"
STATUS_FILE = ROOT / "data" / "worker.json"
WORK_DIR = Path(os.environ.get("TIX_WORK_DIR") or Path.home() / ".local" / "share" / "tix" / "work")
OPENCODE = os.environ.get("TIX_OPENCODE", "opencode")
EXTRA_ARGS = os.environ.get("TIX_OPENCODE_ARGS", "").split()
RUN_TIMEOUT = int(os.environ.get("TIX_RUN_TIMEOUT", "2700"))
FIX_ATTEMPTS = int(os.environ.get("TIX_FIX_ATTEMPTS", "2"))
AUTO_MERGE = os.environ.get("TIX_AUTO_MERGE", "safe")  # safe: unless protected files change | never
MODE = os.environ.get("TIX_WORKER_MODE", "normal")  # normal | fallback (set by run.sh)
POLL_SECONDS = 2

PREAMBLE = """You are the autonomous developer of "tix", the ticket system in the current
repository. A user filed the ticket below in tix itself. Implement it completely in this
repository, following AGENTS.md. Do not ask questions: make reasonable decisions and state
them in your final message. Do not commit; the worker commits for you.
When done, reply with a short summary of what you changed.

"""

CHECKS = [
    ("compile", [sys.executable, "-m", "compileall", "-q", "app", "worker.py", "smoke.py"]),
    ("import", [sys.executable, "-c", "import worker, app.main, app.runs, app.gitops"]),
    ("smoke", [sys.executable, "smoke.py"]),
]


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def child_env() -> dict:
    """Environment for agent and checks: they must resolve paths inside the staging clone."""
    env = dict(os.environ)
    for k in ("TIX_ROOT", "TIX_DB", "TIX_WORKER_MODE", "OLDPWD"):
        env.pop(k, None)
    # opencode takes its working directory from $PWD, not from the process cwd; the inherited
    # PWD points at the live checkout, which made the agent edit it directly.
    env["PWD"] = str(WORK_DIR)
    return env


class RunLog:
    def __init__(self, run_id: int):
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        self.text = open(LOG_DIR / f"run-{run_id}.log", "a", encoding="utf-8")
        self.raw = open(LOG_DIR / f"run-{run_id}.jsonl", "a", encoding="utf-8")
        self.lock = threading.Lock()

    def write(self, msg: str) -> None:
        with self.lock:
            self.text.write(msg.rstrip("\n") + "\n")
            self.text.flush()

    def event(self, line: str) -> None:
        with self.lock:
            self.raw.write(line.rstrip("\n") + "\n")
            self.raw.flush()

    def close(self) -> None:
        self.text.close()
        self.raw.close()


def render_event(ev: dict) -> str | None:
    """Turn an opencode JSON event into a human-readable log line (or None to skip)."""
    kind = ev.get("type")
    part = ev.get("part") or {}
    if kind == "text":
        return part.get("text")
    if kind in ("tool_use", "tool"):
        state = part.get("state") or {}
        title = state.get("title") or json.dumps(state.get("input") or {}, ensure_ascii=False)[:300]
        line = f"▸ [{part.get('tool', '?')}] {title}"
        if state.get("status") == "error":
            line += f"\n    error: {str(state.get('error'))[:1000]}"
        return line
    if kind == "error":
        return "ERROR: " + json.dumps(ev.get("error", ev), ensure_ascii=False)[:2000]
    return None


def session_of(ev: dict) -> str | None:
    return ev.get("sessionID") or (ev.get("part") or {}).get("sessionID")


def cancel_requested(run_id: int) -> bool:
    conn = connect()
    row = conn.execute("SELECT cancel_requested FROM runs WHERE id = ?", (run_id,)).fetchone()
    conn.close()
    return bool(row and row[0])


def run_agent(prompt: str, model: str, session_id: str | None, log: RunLog, run_id: int):
    """Run opencode once in the staging clone. Returns (returncode, session_id, last_text, cancelled)."""
    cmd = [OPENCODE, "run", "--format", "json", "--model", model, "--dir", str(WORK_DIR), *EXTRA_ARGS]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(prompt)
    log.write(f"$ opencode run --model {model}" + (f" --session {session_id}" if session_id else ""))

    proc = subprocess.Popen(
        cmd, cwd=WORK_DIR, env=child_env(), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace", bufsize=1,
        start_new_session=True,
    )
    state = {"session": session_id, "last_text": ""}

    def reader():
        for line in proc.stdout:
            log.event(line)
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                if line.strip():
                    log.write(line)
                continue
            if not isinstance(ev, dict):
                continue
            state["session"] = state["session"] or session_of(ev)
            rendered = render_event(ev)
            if rendered:
                log.write(rendered)
                if ev.get("type") == "text":
                    state["last_text"] = rendered

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    started, cancelled = time.monotonic(), False
    while proc.poll() is None:
        time.sleep(1)
        timed_out = time.monotonic() - started > RUN_TIMEOUT
        if timed_out or cancel_requested(run_id):
            cancelled = True
            log.write("!! timeout reached, stopping agent" if timed_out else "!! cancel requested, stopping agent")
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    t.join(5)
    return proc.returncode, state["session"], state["last_text"], cancelled


def run_checks(log: RunLog) -> tuple[bool, str]:
    """Checks defined here (not in the agent-editable smoke.py alone) gate every change."""
    for name, cmd in CHECKS:
        log.write(f"$ check: {name}")
        try:
            r = subprocess.run(cmd, cwd=WORK_DIR, env=child_env(), capture_output=True, text=True, timeout=180)
            out, ok = (r.stdout + r.stderr).strip(), r.returncode == 0
        except subprocess.TimeoutExpired:
            out, ok = f"check '{name}' timed out after 180s", False
        if out:
            log.write(out)
        if not ok:
            log.write(f"check {name}: FAILED")
            return False, f"[{name}] {out}"
    log.write("checks: OK")
    return True, ""


def finish(conn, run_id: int, status: str, **fields) -> None:
    sets = "".join(f", {k} = ?" for k in fields)
    conn.execute(
        "UPDATE runs SET status = ?, finished_at = datetime('now'),"
        " duration_seconds = CAST((julianday('now') - julianday(started_at)) * 86400 AS INTEGER)"
        f"{sets} WHERE id = ?",
        (status, *fields.values(), run_id),
    )
    ticket_id = conn.execute("SELECT ticket_id FROM runs WHERE id = ?", (run_id,)).fetchone()[0]
    runs.refresh_ticket_status(conn, ticket_id)
    conn.commit()


def keep_attempt(base: str, branch: str, message: str, log: RunLog) -> None:
    """Commit a failed/cancelled attempt on its branch so the diff stays inspectable."""
    if gitops.head(WORK_DIR) != base:
        gitops.git("reset", "-q", "--soft", base, cwd=WORK_DIR)
    if gitops.commit_all(message, cwd=WORK_DIR):
        gitops.fetch_staging(WORK_DIR, branch)
        log.write("Attempt kept on its branch (not merged); see the diff link.")


def live_checkout_touched(run_id: int, log: RunLog) -> bool:
    """Guard: the agent must only write to the staging clone. Stash anything that leaked."""
    if not gitops.is_dirty():
        return False
    with gitops.repo_lock():
        gitops.git("stash", "push", "-u", "-q", "-m", f"tix: live checkout changed during run {run_id}")
    log.write(
        "!! The live checkout changed during this run (the agent wrote outside its staging clone, "
        "or someone edited files by hand). Those changes were moved to `git stash`; nothing was merged."
    )
    return True


def process(run_id: int) -> bool:
    """Handle one run. Returns False if the worker itself hit an unexpected error."""
    conn = connect()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    t = conn.execute("SELECT * FROM tickets WHERE id = ?", (run["ticket_id"],)).fetchone()
    branch = f"tix/run-{run_id}"
    conn.execute(
        "UPDATE runs SET status = 'running', started_at = datetime('now'), branch = ? WHERE id = ?",
        (branch, run_id),
    )
    runs.refresh_ticket_status(conn, t["id"])
    conn.commit()

    log = RunLog(run_id)
    try:
        sha = gitops.commit_manual_changes(f"before ticket #{t['id']}")
        if sha:
            log.write(f"Committed pending manual changes in the live checkout as {sha[:10]}.")
        pending = conn.execute(
            "SELECT branch FROM runs WHERE ticket_id = ? AND status = 'review' ORDER BY id DESC LIMIT 1",
            (t["id"],),
        ).fetchone()
        base_ref = pending["branch"] if pending and gitops.branch_exists(pending["branch"]) else "HEAD"
        base = gitops.create_staging(WORK_DIR, branch, base_ref)
        copy_db(db_path(), WORK_DIR / "data" / "tickets.db")
        log.write(f"Staging clone {WORK_DIR} on {branch} (from {base_ref}, {base[:10]}) with a copy of the DB.")

        first = t["session_id"] is None
        prompt = PREAMBLE + run["prompt"] if first else (
            f"Follow-up from the user on ticket #{t['id']} ({t['title']}):\n\n{run['prompt']}\n\n"
            "Implement this, then reply with a short summary of what you changed."
        )
        rc, sid, summary, cancelled = run_agent(prompt, t["model"], t["session_id"], log, run_id)
        if sid and sid != t["session_id"]:
            conn.execute("UPDATE tickets SET session_id = ? WHERE id = ?", (sid, t["id"]))
            conn.commit()

        if cancelled or rc != 0:
            live_checkout_touched(run_id, log)
            keep_attempt(base, branch, f"tix run {run_id} (ticket #{t['id']}): unfinished attempt", log)
            if cancelled:
                finish(conn, run_id, "cancelled", error="Cancelled or timed out.")
            else:
                finish(conn, run_id, "failed", error=f"opencode exited with code {rc}; see log.")
            return True

        ok, out = run_checks(log)
        for attempt in range(FIX_ATTEMPTS):
            if ok:
                break
            log.write(f"-- asking agent to fix the failing check (attempt {attempt + 1}/{FIX_ATTEMPTS})")
            fix_prompt = (
                "After your change, a required check fails:\n\n"
                f"{out[-6000:]}\n\nFix the problem so all checks (compile, import, `python smoke.py`) pass, "
                "without dropping the ticket's goal."
            )
            rc, sid, s2, cancelled = run_agent(fix_prompt, t["model"], sid, log, run_id)
            summary = s2 or summary
            if cancelled or rc != 0:
                break
            ok, out = run_checks(log)

        if live_checkout_touched(run_id, log):
            keep_attempt(base, branch, f"tix run {run_id} (ticket #{t['id']}): wrote to live checkout", log)
            finish(conn, run_id, "failed", summary=summary,
                   error="The live checkout changed during the run; those changes were stashed (see log).")
            return True

        if not ok:
            keep_attempt(base, branch, f"tix run {run_id} (ticket #{t['id']}): failed checks", log)
            finish(conn, run_id, "failed", summary=summary, error="Checks failed:\n" + out[-4000:])
            return True

        if gitops.head(WORK_DIR) != base:  # the agent committed on its own; squash into one commit
            gitops.git("reset", "-q", "--soft", base, cwd=WORK_DIR)
        gitops.commit_all(f"#{t['id']} {t['title']}\n\n{summary[:3000]}\n\ntix run {run_id}", cwd=WORK_DIR)
        gitops.fetch_staging(WORK_DIR, branch)

        files = gitops.changed_files(branch)
        if not files:
            gitops.delete_branch(branch)
            log.write("Agent made no file changes.")
            runs.supersede_older(conn, run)
            finish(conn, run_id, "done", summary=summary, branch=None)
            return True

        protected = runs.protected_changes(files)
        runs.supersede_older(conn, run)
        if protected:
            reason = "Changes protected files: " + ", ".join(protected)
        elif AUTO_MERGE == "never":
            reason = "Auto-merge is disabled (TIX_AUTO_MERGE=never)."
        else:
            finish(conn, run_id, "review", summary=summary)
            merged, err = runs.merge_run(conn, run_id)
            log.write("Checks passed; merged into the live checkout." if merged else f"Auto-merge failed:\n{err}")
            return True
        log.write(f"Checks passed; waiting for approval. {reason}")
        finish(conn, run_id, "review", summary=summary, review_reason=reason)
        return True
    except Exception:
        err = traceback.format_exc()
        log.write(err)
        finish(conn, run_id, "failed", error=err[-4000:])
        return False
    finally:
        gitops.remove_staging(WORK_DIR)
        log.close()
        conn.close()


def recover_interrupted(conn) -> None:
    """Runs left 'running' by a crashed/stopped worker: fail them, drop the staging clone."""
    stale = conn.execute("SELECT id FROM runs WHERE status = 'running'").fetchall()
    for r in stale:
        finish(conn, r["id"], "failed", error="Worker stopped during this run.")
    if stale:
        gitops.remove_staging(WORK_DIR)


def write_status(head: str | None) -> None:
    STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATUS_FILE.write_text(json.dumps({"mode": MODE, "pid": os.getpid(), "head": head, "since": time.time()}))


def restart() -> None:
    if MODE == "fallback":
        sys.exit(0)  # run.sh then retries the current worker
    os.execv(sys.executable, [sys.executable, *sys.argv])


def main() -> None:
    load_env()
    start_head = gitops.head()
    conn = connect()
    migrate(conn)
    recover_interrupted(conn)
    conn.close()
    if MODE == "normal" and not gitops.has_tag(gitops.GOOD_WORKER_TAG):
        gitops.tag_good_worker(start_head)
    write_status(start_head)
    print(f"[worker] ready (pid {os.getpid()}, mode {MODE}, work dir {WORK_DIR})", flush=True)
    while True:
        if gitops.head() != start_head:
            print("[worker] live HEAD moved; restarting to pick up code changes", flush=True)
            restart()
        conn = connect()
        row = conn.execute("SELECT id FROM runs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        conn.close()
        if not row:
            time.sleep(POLL_SECONDS)
            continue
        print(f"[worker] processing run {row['id']}", flush=True)
        healthy = process(row["id"])
        if healthy and MODE == "normal":
            gitops.tag_good_worker(start_head)
        print(f"[worker] run {row['id']} finished; restarting", flush=True)
        restart()


if __name__ == "__main__":
    main()

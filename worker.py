"""Agent worker: picks queued runs and lets opencode implement them in this repo.

For each run:
  1. commit any manual (uncommitted) changes so the agent starts from a clean tree
  2. run `opencode run` with the ticket as prompt (follow-ups continue the session)
  3. run smoke.py; if it fails, ask the agent to fix it (up to FIX_ATTEMPTS times)
  4. on success commit everything as one commit; on failure stash the changes away
The worker re-executes itself after every run so changes to its own code take effect.
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

from app import gitops
from app.db import ROOT, connect, migrate

LOG_DIR = ROOT / "data" / "logs"
OPENCODE = os.environ.get("TIX_OPENCODE", "opencode")
EXTRA_ARGS = os.environ.get("TIX_OPENCODE_ARGS", "").split()
RUN_TIMEOUT = int(os.environ.get("TIX_RUN_TIMEOUT", "2700"))
FIX_ATTEMPTS = int(os.environ.get("TIX_FIX_ATTEMPTS", "2"))
POLL_SECONDS = 2

PREAMBLE = """You are the autonomous developer of "tix", the ticket system in the current
repository. A user filed the ticket below in tix itself. Implement it completely in this
repository, following AGENTS.md. Do not ask questions: make reasonable decisions and state
them in your final message. Do not commit; the worker commits for you.
When done, reply with a short summary of what you changed.

"""


def load_env() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


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
    """Run opencode once. Returns (returncode, session_id, last_text, cancelled)."""
    cmd = [OPENCODE, "run", "--format", "json", "--model", model, *EXTRA_ARGS]
    if session_id:
        cmd += ["--session", session_id]
    cmd.append(prompt)
    log.write(f"$ opencode run --model {model}" + (f" --session {session_id}" if session_id else ""))

    proc = subprocess.Popen(
        cmd, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, start_new_session=True,
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


def smoke(log: RunLog) -> tuple[bool, str]:
    log.write("$ python smoke.py")
    try:
        r = subprocess.run(
            [sys.executable, "smoke.py"], cwd=ROOT, capture_output=True, text=True, timeout=180
        )
        out, ok = (r.stdout + r.stderr).strip(), r.returncode == 0
    except subprocess.TimeoutExpired:
        out, ok = "smoke.py timed out after 180s", False
    log.write(out or "(no output)")
    log.write("smoke: OK" if ok else "smoke: FAILED")
    return ok, out


def discard(base: str, label: str, log: RunLog) -> None:
    """Undo the agent's work but keep it recoverable in `git stash list`."""
    if gitops.head() != base:
        gitops.git("reset", "-q", "--soft", base)
    if gitops.is_dirty():
        gitops.git("stash", "push", "-u", "-q", "-m", label)
        log.write(f"Changes stashed as '{label}' (see `git stash list`).")


def finish(conn, run_id: int, ticket_id: int, status: str, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(
        f"UPDATE runs SET status = ?, finished_at = datetime('now'){', ' + sets if sets else ''} WHERE id = ?",
        (status, *fields.values(), run_id),
    )
    # A cancelled run only cancels the ticket if nothing else is pending for it.
    pending = conn.execute(
        "SELECT 1 FROM runs WHERE ticket_id = ? AND status = 'queued'", (ticket_id,)
    ).fetchone()
    if not pending:
        conn.execute(
            "UPDATE tickets SET status = ?, updated_at = datetime('now') WHERE id = ?", (status, ticket_id)
        )
    conn.commit()


def process(run_id: int) -> None:
    conn = connect()
    run = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
    t = conn.execute("SELECT * FROM tickets WHERE id = ?", (run["ticket_id"],)).fetchone()
    conn.execute("UPDATE runs SET status = 'running', started_at = datetime('now') WHERE id = ?", (run_id,))
    conn.execute("UPDATE tickets SET status = 'running', updated_at = datetime('now') WHERE id = ?", (t["id"],))
    conn.commit()

    log = RunLog(run_id)
    try:
        if gitops.is_dirty():
            sha = gitops.commit_all(f"Manual changes (auto-committed before ticket #{t['id']})")
            log.write(f"Committed pending manual changes as {sha[:10]}.")
        base = gitops.head()

        first = t["session_id"] is None
        prompt = PREAMBLE + run["prompt"] if first else (
            f"Follow-up from the user on ticket #{t['id']} ({t['title']}):\n\n{run['prompt']}\n\n"
            "Implement this, then reply with a short summary of what you changed."
        )
        rc, sid, summary, cancelled = run_agent(prompt, t["model"], t["session_id"], log, run_id)
        if sid and sid != t["session_id"]:
            conn.execute("UPDATE tickets SET session_id = ? WHERE id = ?", (sid, t["id"]))
            conn.commit()

        if cancelled:
            discard(base, f"tix: cancelled run {run_id} (ticket #{t['id']})", log)
            return finish(conn, run_id, t["id"], "cancelled", error="Cancelled or timed out.")
        if rc != 0:
            discard(base, f"tix: failed run {run_id} (ticket #{t['id']})", log)
            return finish(conn, run_id, t["id"], "failed", error=f"opencode exited with code {rc}; see log.")

        ok, out = smoke(log)
        for attempt in range(FIX_ATTEMPTS):
            if ok:
                break
            log.write(f"-- asking agent to fix smoke failure (attempt {attempt + 1}/{FIX_ATTEMPTS})")
            fix_prompt = (
                "After your change, the smoke test (`python smoke.py`) fails with:\n\n"
                f"{out[-6000:]}\n\nFix the problem so smoke.py passes, without dropping the ticket's goal."
            )
            rc, sid, s2, cancelled = run_agent(fix_prompt, t["model"], sid, log, run_id)
            summary = s2 or summary
            if cancelled or rc != 0:
                break
            ok, out = smoke(log)

        if not ok:
            discard(base, f"tix: failed run {run_id} (ticket #{t['id']})", log)
            return finish(conn, run_id, t["id"], "failed", summary=summary, error="Smoke test failed:\n" + out[-4000:])

        if gitops.head() != base:  # the agent committed on its own; squash into our commit
            gitops.git("reset", "-q", "--soft", base)
        sha = gitops.commit_all(f"#{t['id']} {t['title']}\n\n{summary[:3000]}\n\ntix run {run_id}")
        log.write(f"Committed {sha[:10]}." if sha else "Agent made no file changes.")
        finish(conn, run_id, t["id"], "done", summary=summary, commit_sha=sha)
    except Exception:
        err = traceback.format_exc()
        log.write(err)
        finish(conn, run_id, t["id"], "failed", error=err[-4000:])
    finally:
        log.close()
        conn.close()


def recover_interrupted(conn) -> None:
    """Runs left 'running' by a crashed/stopped worker: fail them and stash leftovers."""
    stale = conn.execute("SELECT id, ticket_id FROM runs WHERE status = 'running'").fetchall()
    for r in stale:
        if gitops.is_dirty():
            gitops.git("stash", "push", "-u", "-q", "-m", f"tix: interrupted run {r['id']}")
        finish(conn, r["id"], r["ticket_id"], "failed", error="Worker stopped during this run; changes stashed.")


def main() -> None:
    load_env()
    conn = connect()
    migrate(conn)
    recover_interrupted(conn)
    conn.close()
    print(f"[worker] ready (pid {os.getpid()}), polling for queued runs", flush=True)
    while True:
        conn = connect()
        row = conn.execute("SELECT id FROM runs WHERE status = 'queued' ORDER BY id LIMIT 1").fetchone()
        conn.close()
        if not row:
            time.sleep(POLL_SECONDS)
            continue
        print(f"[worker] processing run {row['id']}", flush=True)
        process(row["id"])
        print(f"[worker] run {row['id']} finished; restarting to pick up code changes", flush=True)
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    main()

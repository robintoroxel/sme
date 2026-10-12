"""Small git helpers shared by the web app (revert) and the worker (commit)."""
import subprocess

from .db import ROOT

IDENTITY = ["-c", "user.name=tix-agent", "-c", "user.email=tix-agent@localhost"]


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *IDENTITY, *args], cwd=ROOT, capture_output=True, text=True, check=check
    )


def head() -> str | None:
    r = git("rev-parse", "HEAD", check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def is_dirty() -> bool:
    return bool(git("status", "--porcelain").stdout.strip())


def commit_all(message: str) -> str | None:
    """Stage everything and commit. Returns the new sha, or None if nothing changed."""
    git("add", "-A")
    if not git("diff", "--cached", "--name-only").stdout.strip():
        return None
    git("commit", "-q", "-m", message)
    return head()


def rollback_to(sha: str) -> None:
    git("reset", "-q", "--hard", sha)
    git("clean", "-q", "-fd")  # untracked files only; ignored files (data/, .env) are kept


def revert(shas: list[str]) -> tuple[bool, str]:
    """Revert the given commits (newest first). Aborts cleanly on conflict."""
    for sha in shas:
        r = git("revert", "--no-edit", sha, check=False)
        if r.returncode != 0:
            git("revert", "--abort", check=False)
            return False, (r.stdout + r.stderr).strip()
    return True, ""


def show(sha: str) -> str:
    r = git("show", "--stat", "--patch", "--no-color", sha, check=False)
    return r.stdout if r.returncode == 0 else r.stderr

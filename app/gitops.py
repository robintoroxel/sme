"""Git helpers shared by the web app and the worker.

The live checkout (ROOT) only ever receives finished, checked work: agents work in a
separate worktree on a branch `tix/run-N`, which is squash-merged into the live
branch when approved. All mutations of the live checkout go through repo_lock().
"""
import contextlib
import fcntl
import hashlib
import shutil
import subprocess
import tempfile
from pathlib import Path

from .db import ROOT

IDENTITY = ["-c", "user.name=tix-agent", "-c", "user.email=tix-agent@localhost"]
GOOD_WORKER_TAG = "tix-worker-good"


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *IDENTITY, *args], cwd=cwd or ROOT, capture_output=True, text=True, check=check
    )


@contextlib.contextmanager
def repo_lock():
    """Serialise changes to the live checkout between the web app and the worker."""
    key = hashlib.sha1(str(ROOT).encode()).hexdigest()[:12]
    with open(Path(tempfile.gettempdir()) / f"tix-{key}.lock", "w") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


def head(cwd: Path | None = None) -> str | None:
    r = git("rev-parse", "HEAD", cwd=cwd, check=False)
    return r.stdout.strip() if r.returncode == 0 else None


def is_dirty(cwd: Path | None = None) -> bool:
    return bool(git("status", "--porcelain", cwd=cwd).stdout.strip())


def commit_all(message: str, cwd: Path | None = None) -> str | None:
    """Stage everything and commit. Returns the new sha, or None if nothing changed."""
    git("add", "-A", cwd=cwd)
    if not git("diff", "--cached", "--name-only", cwd=cwd).stdout.strip():
        return None
    git("commit", "-q", "-m", message, cwd=cwd)
    return head(cwd)


def commit_manual_changes(reason: str) -> str | None:
    with repo_lock():
        if is_dirty():
            return commit_all(f"Manual changes (auto-committed {reason})")
    return None


# --- worktrees and run branches -------------------------------------------------

def remove_worktree(path: Path) -> None:
    git("worktree", "remove", "--force", str(path), check=False)
    shutil.rmtree(path, ignore_errors=True)
    git("worktree", "prune", check=False)


def create_worktree(path: Path, branch: str, base: str) -> None:
    remove_worktree(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    git("worktree", "add", "-q", "-B", branch, str(path), base)


def branch_exists(branch: str) -> bool:
    return git("rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False).returncode == 0


def delete_branch(branch: str) -> None:
    git("branch", "-D", branch, check=False)


def changed_files(branch: str) -> list[str]:
    """Files a branch changes relative to the live HEAD."""
    out = git("diff", "--name-only", f"HEAD...{branch}").stdout
    return [line for line in out.splitlines() if line]


def branch_diff(branch: str) -> str:
    r = git("diff", "--stat", "--patch", "--no-color", f"HEAD...{branch}", check=False)
    return r.stdout if r.returncode == 0 else r.stderr


def merge_squash(branch: str, message: str) -> tuple[str | None, str]:
    """Squash-merge a run branch into the live checkout. Returns (sha, error)."""
    with repo_lock():
        if is_dirty():
            commit_all("Manual changes (auto-committed before merge)")
        r = git("merge", "--squash", branch, check=False)
        if r.returncode != 0:
            git("reset", "-q", "--hard", "HEAD")  # the tree was clean before the merge attempt
            return None, (r.stdout + r.stderr).strip()
        sha = commit_all(message)
        delete_branch(branch)
        return sha, ""


def revert(shas: list[str]) -> tuple[bool, str]:
    """Revert the given commits (newest first). Aborts cleanly on conflict."""
    with repo_lock():
        for sha in shas:
            r = git("revert", "--no-edit", sha, check=False)
            if r.returncode != 0:
                git("revert", "--abort", check=False)
                return False, (r.stdout + r.stderr).strip()
    return True, ""


def show(sha: str) -> str:
    r = git("show", "--stat", "--patch", "--no-color", sha, check=False)
    return r.stdout if r.returncode == 0 else r.stderr


def tag_good_worker(sha: str) -> None:
    git("tag", "-f", GOOD_WORKER_TAG, sha, check=False)


def has_tag(tag: str) -> bool:
    return git("rev-parse", "--verify", "-q", f"refs/tags/{tag}", check=False).returncode == 0

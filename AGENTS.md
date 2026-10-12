# tix: agent guide

tix is a small ticket system. Tickets filed in tix are implemented by an opencode agent,
which is how tix develops itself. You are probably that agent.

## Where you are working
You work in a **staging worktree**: a separate checkout on branch `tix/run-N`, with a copy
of the live database in `data/tickets.db`. The live app is not affected while you work.
When you finish, the worker runs the checks (compile, import, `python smoke.py`). If they
pass, your change is merged into the live app, or held for human approval when it touches
protected files (see below). You can experiment freely, but only files in this worktree count.

## Layout
- `app/main.py`: FastAPI routes (server-rendered HTML plus a tiny JSON API)
- `app/templates/*.html`: Jinja2 templates; `app/static/style.css`: all styling (no build step, no JS framework)
- `app/db.py`: SQLite connection plus the migration runner
- `app/migrations/NNN_name.sql`: schema migrations, applied once each in filename order
- `app/runs.py`: run/ticket status transitions, merge/approve/reject
- `app/gitops.py`: git helpers (worktrees, merge, revert)
- `worker.py`: picks queued runs and invokes opencode (that is you)
- `smoke.py`: post-change check; must pass
- `data/`: the database copy and logs (git-ignored)

## Rules
- **DB schema changes:** add a NEW migration file such as `app/migrations/005_add_labels.sql`,
  numbered after the highest existing one. Never edit a migration that already exists, because it
  has already been applied to the live DB.
  SQLite has limited `ALTER TABLE`, so use `ALTER TABLE ... ADD COLUMN` or the create-copy-drop-rename pattern.
  Keep existing data intact. Look at the actual schema first, because a reverted ticket can leave columns behind:
  `uv run python -c "import sqlite3;[print(r[0]) for r in sqlite3.connect('data/tickets.db').execute('select sql from sqlite_master where sql is not null')]"`
- Do **not** start servers or long-running processes.
- Verify your work with `uv run python smoke.py`. When you add a significant feature, extend `smoke.py` to cover it.
- Add Python dependencies with `uv add <pkg>`. Do not add frontend build tooling.
- Do not run `git commit`, `git reset`, `git checkout` or `git push`; the worker handles git.
- Do not read or print `.env` (it holds secrets), and do not touch anything outside this directory.
- Keep the style consistent: plain functions, short modules, server-rendered templates, CSS variables in `style.css`.
- **Protected files** (`worker.py`, `smoke.py`, `run.sh`, `AGENTS.md`, `opencode.json`, `app/db.py`,
  `app/gitops.py`, `app/runs.py`) may be changed when the ticket needs it, but every such change waits
  for human approval. Do not weaken checks or permissions to get a change through.

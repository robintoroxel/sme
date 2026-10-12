# tix: agent guide

tix is a small ticket system. Tickets filed in tix are implemented by an opencode agent
working in this repository, which is how tix develops itself. You are probably that agent.

## Layout
- `app/main.py`: FastAPI routes (server-rendered HTML plus a tiny JSON API)
- `app/templates/*.html`: Jinja2 templates; `app/static/style.css`: all styling (no build step, no JS framework)
- `app/db.py`: SQLite connection plus the migration runner
- `app/migrations/NNN_name.sql`: schema migrations, applied once each in filename order
- `app/gitops.py`: git helpers used for commit/revert
- `worker.py`: picks queued runs and invokes opencode (that is you)
- `smoke.py`: post-change check; must pass or your change is rolled back
- `data/`: live database and run logs (git-ignored). **Never edit or delete anything in `data/`.**

## Rules
- **DB schema changes:** add a NEW migration file such as `app/migrations/002_add_priority.sql`.
  Never edit a migration that already exists, because it has already been applied to the live DB.
  SQLite has limited `ALTER TABLE`, so use `ALTER TABLE ... ADD COLUMN` or the create-copy-drop-rename pattern.
  Keep existing data intact.
- The web server is already running with auto-reload, so do **not** start servers or long-running processes.
- Verify your work with `python smoke.py` (run from the repo root inside the project venv: `uv run python smoke.py`).
  When you add a significant feature, extend `smoke.py` to cover it.
- Add Python dependencies with `uv add <pkg>`. Do not add frontend build tooling.
- Do not run `git commit`, `git reset`, `git checkout` or `git push`; the worker handles git.
- Do not read or print `.env` (it holds secrets).
- Keep the style consistent: plain functions, short modules, server-rendered templates, CSS variables in `style.css`.
- Changing `worker.py` is allowed; the worker restarts itself after each run. Be careful,
  because a broken worker cannot fix itself.

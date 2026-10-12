# tix

A ticket system that develops itself. File a ticket in the web UI and an
[opencode](https://opencode.ai) agent (DeepSeek V4) implements it in this repository.

## Run (inside WSL)

```bash
cd /mnt/c/Users/rtoroxel_ailio/erp
bash run.sh            # web UI on http://localhost:8000, plus the agent worker
```

`run.sh` needs `uv` and `opencode` on PATH, and `DEEPSEEK_ERP_API_KEY` set in `.env`.

## How it works
- Each ticket, and each follow-up comment, becomes a **run**. `worker.py` handles runs one at a time.
- A run calls `opencode run --model deepseek/deepseek-v4-pro`. Follow-ups continue the same opencode session.
- After the agent finishes, `smoke.py` checks the app against a copy of the DB. If the check fails,
  the agent gets the error and up to 2 tries to fix it. If it still fails, its changes are stashed (`git stash list`).
- A successful run becomes one git commit, which you can view in the UI as a diff and **revert** from the ticket page.
- Schema changes go in new files under `app/migrations/`, applied automatically.
- The web server reloads on code changes. The worker restarts itself after every run.

Agent rules live in `AGENTS.md`; model and permissions live in `opencode.json`.

Environment knobs: `PORT`, `TIX_RUN_TIMEOUT` (seconds, default 2700), `TIX_FIX_ATTEMPTS` (default 2),
`TIX_OPENCODE_ARGS` (extra flags for `opencode run`).

# tix

A ticket system that develops itself. File a ticket in the web UI and an
[opencode](https://opencode.ai) agent (DeepSeek V4) implements it in this repository.

## Run (inside WSL)

```bash
cd /mnt/c/Users/rtoroxel_ailio/erp
bash run.sh            # web UI on http://localhost:8000, plus the supervised agent worker
```

`run.sh` needs `uv` and `opencode` on PATH, and `DEEPSEEK_ERP_API_KEY` set in `.env`.

## How a ticket flows
1. Each ticket, and each follow-up comment, becomes a **run**. `worker.py` handles runs one at a time.
2. The agent works in a **staging clone** (`~/.local/share/tix/work`, branch `tix/run-N`, with its own
   `.git`, so opencode treats it as a separate project) and a
   copy of the live DB. The live app and DB are untouched while it works.
3. Checks: compile, import, then `smoke.py`. If a check fails, the agent gets the error and up to 2 tries to fix it.
   If it still fails, the run fails and the attempt is kept on its branch, where you can view the diff.
4. If the checks pass, the change is **squash-merged** into the live checkout as one commit. The web server
   reloads and applies new migrations to the live DB. The exceptions are changes to **protected files**
   (worker, checks, agent rules, permissions, git/DB plumbing) and `TIX_AUTO_MERGE=never`: those wait
   in status **review** for you to **Approve** or **Reject**.
5. A follow-up on a ticket awaiting review builds on that unmerged branch. A merged ticket can be **reverted**.

## Self-reference safeguards
- The agent never edits the live checkout, so a half-finished change can't take the app down or migrate the live DB. If the live checkout changes during a run anyway, those changes are stashed and the run fails.
- The worker defines its own checks, so the agent can't get around them by editing `smoke.py`.
- The agent can't change its own guardrails without approval: edits to `worker.py`, `smoke.py`, `run.sh`,
  `AGENTS.md`, `opencode.json`, `app/db.py`, `app/gitops.py` and `app/runs.py` always need it.
- `run.sh` restarts a crashed worker. After 3 quick crashes in a row it runs the last known-good
  worker (git tag `tix-worker-good`, moved after every healthy run), so a ticket can still fix the broken one.
  The UI shows a banner while this fallback is active.
- The UI warns about migrations applied to the live DB whose file has since been reverted.

Agent rules live in `AGENTS.md`; model and permissions live in `opencode.json`.

Environment knobs: `PORT`, `TIX_AUTO_MERGE` (`safe` default | `never`), `TIX_RUN_TIMEOUT` (seconds, default 2700),
`TIX_FIX_ATTEMPTS` (default 2), `TIX_WORK_DIR`, `TIX_OPENCODE_ARGS` (extra flags for `opencode run`).

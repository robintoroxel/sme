#!/usr/bin/env bash
# Start tix (web UI + agent worker). Run inside WSL:  ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

# Keep the virtualenv on the Linux filesystem (fast, and separate from any Windows tooling).
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/tix}"
export UV_LINK_MODE=copy
uv sync -q

if [ ! -d .git ]; then
  git init -q -b main
  git add -A
  git -c user.name=tix-agent -c user.email=tix-agent@localhost commit -q -m "Initial ticket system"
fi

set -a; . ./.env; set +a
# inotify does not work for /mnt/c in WSL, so the reloader has to poll.
export WATCHFILES_FORCE_POLLING=true

PORT="${PORT:-8000}"
uv run uvicorn app.main:app --host 127.0.0.1 --port "$PORT" --reload --reload-dir app &
WEB=$!
uv run python worker.py &
WORKER=$!
trap 'kill $WEB $WORKER 2>/dev/null; wait' EXIT INT TERM

echo "tix running at http://localhost:$PORT  (Ctrl+C to stop)"
wait -n

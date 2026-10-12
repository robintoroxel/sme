#!/usr/bin/env bash
# Start tix (web UI + supervised agent worker). Run inside WSL:  bash run.sh
set -uo pipefail
cd "$(dirname "$0")"

# Keep the virtualenv on the Linux filesystem (fast, and separate from any Windows tooling).
export UV_PROJECT_ENVIRONMENT="${UV_PROJECT_ENVIRONMENT:-$HOME/.venvs/tix}"
export UV_LINK_MODE=copy
uv sync -q || exit 1

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

# Supervise the worker. If it crashes 3 times in a row within 60s of starting, run the
# last known-good version (git tag tix-worker-good) so tickets can still fix the broken one.
supervise_worker() {
  local fails=0 start code
  while true; do
    start=$(date +%s)
    if [ "$fails" -ge 3 ] && git rev-parse -q --verify refs/tags/tix-worker-good >/dev/null; then
      echo "[run.sh] worker keeps crashing; starting last known-good worker (tix-worker-good)"
      rm -rf data/fallback && mkdir -p data/fallback
      git archive tix-worker-good worker.py app | tar -x -C data/fallback
      TIX_ROOT="$PWD" TIX_WORKER_MODE=fallback uv run python data/fallback/worker.py
    else
      uv run python worker.py
    fi
    code=$?
    if [ "$code" -eq 0 ] || [ $(( $(date +%s) - start )) -ge 60 ]; then fails=0; else fails=$((fails + 1)); fi
    echo "[run.sh] worker exited with code $code (quick failures: $fails); restarting"
    sleep 2
  done
}
supervise_worker &
SUP=$!
trap 'kill $WEB $SUP 2>/dev/null; pkill -P $SUP 2>/dev/null; wait' EXIT INT TERM

echo "tix running at http://localhost:$PORT  (Ctrl+C to stop)"
wait $WEB

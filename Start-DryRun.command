#!/bin/zsh
set -u

# Double-click in Finder. Tailscale must be connected on this Mac.
PROJECT_DIR="${0:A:h}"
PYTHON_BIN="$PROJECT_DIR/../.venv/bin/python"
MONITOR_HOST="tailscale"
MONITOR_PORT="8765"

cd "$PROJECT_DIR" || exit 1
if [[ ! -x "$PYTHON_BIN" ]]; then
  print -r -- "Python virtual environment not found: $PYTHON_BIN"
  read -r "?Press Enter to close..."
  exit 1
fi

print -r -- "TradingAgents: DRY RUN (simulated account; no real Toss orders)"
print -r -- "Default: hourly analysis from 10:00; BUY before 14:30. Same-day exits; approved carry exits by next-session 10:00. Saved web settings apply."
print -r -- "Dashboard URL will appear below."
print -r -- "Press Ctrl+C to stop. Keep this Terminal window open."

# Keep the Mac awake while the scheduler runs. Display sleep is still allowed.
# Fixed mode flags follow extra arguments so this launcher stays a dry run.
/usr/bin/caffeinate -i "$PYTHON_BIN" -u main.py "$@" \
  --scenario auto-paper --run-mode daily-market \
  --monitor --monitor-host "$MONITOR_HOST" --monitor-port "$MONITOR_PORT"
run_exit_code=$?

print -r -- "Dry run stopped (exit code: $run_exit_code)."
if [[ -t 0 ]]; then
  read -r "?Press Enter to close..."
fi
exit "$run_exit_code"

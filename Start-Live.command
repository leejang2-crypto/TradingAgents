#!/bin/zsh
set -u

# REAL MONEY: same candidate/analysis/schedule/settings pipeline as dry-run,
# with actual Toss orders at the final execution stage.

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

print -r -- "TradingAgents: LIVE TRADING - REAL TOSS ORDERS"
print -r -- "Daily market schedule, automatic candidates, saved web budget and LLM settings."
print -r -- "HOLD sends no order. Dashboard URL will appear below."
print -r -- "Press Ctrl+C to stop. Keep this Terminal window open."

# --yes is the existing explicit live-order authorization. Do not use this
# launcher for a paper test. main.py loads API credentials from the shared .env.
/usr/bin/caffeinate -i "$PYTHON_BIN" -u main.py "$@" \
  --scenario auto-live --run-mode daily-market --yes \
  --monitor --monitor-host "$MONITOR_HOST" --monitor-port "$MONITOR_PORT"
run_exit_code=$?

print -r -- "Live run finished (exit code: $run_exit_code)."
if [[ -t 0 ]]; then
  read -r "?Press Enter to close..."
fi
exit "$run_exit_code"

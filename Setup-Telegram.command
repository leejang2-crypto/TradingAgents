#!/bin/zsh
set -u
PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR" || exit 1
"$PROJECT_DIR/../.venv/bin/python" -m tradingagents.monitoring.telegram_setup
result=$?
read -r "?Press Enter to close..."
exit "$result"

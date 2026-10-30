#!/bin/zsh
set -u
PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR" || exit 1
print -r -- "Postmarket archive: /Volumes/External/TradingAgents/postmarket"
print -r -- "Allow Terminal access to removable volumes if macOS asks. No orders or API calls."
"$PROJECT_DIR/../.venv/bin/python" -c 'from tradingagents.research.postmarket import archive_available, ARCHIVE_ROOT; archive_available(); print("Ready:", ARCHIVE_ROOT)'
result=$?
if [[ $result -ne 0 ]]; then
  print -r -- "Check External is mounted, then allow Terminal access in macOS Privacy & Security > Files and Folders."
fi
read -r "?Press Enter to close..."
exit "$result"

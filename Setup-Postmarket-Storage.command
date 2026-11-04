#!/bin/zsh
set -u
PROJECT_DIR="${0:A:h}"
cd "$PROJECT_DIR" || exit 1
print -r -- "Postmarket archive: /Volumes/External/TradingAgents/postmarket"
print -r -- "Weekly archive: /Volumes/External/TradingAgents/weekly_review"
print -r -- "Strategy research: /Volumes/External/TradingAgents/strategy_research"
print -r -- "Allow Terminal access to removable volumes if macOS asks. No orders or API calls."
"$PROJECT_DIR/../.venv/bin/python" -c 'from tradingagents.research import postmarket, weekly_review, strategy_comparison; postmarket.archive_available(); weekly_review.archive_available(); strategy_comparison.archive_available(); print("Ready:", postmarket.ARCHIVE_ROOT, weekly_review.ARCHIVE_ROOT, strategy_comparison.ARCHIVE_ROOT)'
result=$?
if [[ $result -ne 0 ]]; then
  print -r -- "Check External is mounted, then allow Terminal access in macOS Privacy & Security > Files and Folders."
fi
read -r "?Press Enter to close..."
exit "$result"

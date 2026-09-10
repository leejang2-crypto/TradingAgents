"""Inspect experience memory and add sourced human reviews, without LLM calls.

Run from the repository: python -m tradingagents.learning --help
"""

import argparse
import json
from pathlib import Path

from tradingagents.agents.utils.experience import ExperienceStore, ROLE_FIELDS


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, default=Path(__file__).resolve().parents[1] / ".local" / "learning" / "experiences.sqlite3")
    commands = parser.add_subparsers(dest="command", required=True)
    recent = commands.add_parser("list", help="List recent dry-run or live experiences.")
    recent.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    show = commands.add_parser("show", help="Show all reports, execution events and feedback.")
    show.add_argument("run_id")
    review = commands.add_parser("feedback", help="Add a sourced human review, not a verified profit label.")
    review.add_argument("run_id")
    review.add_argument("--agent", choices=list(ROLE_FIELDS), required=True)
    review.add_argument("--lesson", required=True)
    review.add_argument("--source", required=True)
    args = parser.parse_args(argv)
    store = ExperienceStore(args.database)
    if args.command == "list":
        result = store.recent(args.mode)
    elif args.command == "show":
        result = store.get(args.run_id)
    else:
        store.add_feedback(args.run_id, args.agent, args.lesson, args.source)
        result = {"saved": True, "runId": args.run_id, "kind": "human-review"}
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

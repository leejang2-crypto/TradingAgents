from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.naver_news import (
    collect_candidate_news_naver,
    rank_candidate_tickers,
)


PROJECT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_DIR / ".local" / "candidates"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Discover Korean stock candidates from Naver Search API news keywords."
    )
    parser.add_argument("--start-date", help="Start date YYYY-MM-DD. Default: end-date - days.")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y-%m-%d"), help="End date YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=3, help="Lookback days when --start-date is omitted.")
    parser.add_argument(
        "--categories",
        default="market,themes,selection_signals,risk_signals",
        help="Comma-separated keyword buckets to collect.",
    )
    parser.add_argument(
        "--extra-queries",
        default="",
        help="Comma-separated custom Naver news queries to include.",
    )
    parser.add_argument("--display", type=int, default=10, help="Naver display count per query, max 100.")
    parser.add_argument("--sort", choices=["date", "sim"], default="date", help="Naver news sort mode.")
    parser.add_argument("--limit", type=int, default=20, help="Number of ranked candidates to keep.")
    parser.add_argument(
        "--format",
        choices=["json", "md", "both"],
        default="both",
        help="Output format.",
    )
    parser.add_argument("--output-prefix", help="Output path without extension.")
    return parser


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_start(end_date: str, days: int) -> str:
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    return (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")


def _output_prefix(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"naver_candidates_{stamp}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Naver Candidate Discovery",
        "",
        f"- Period: {payload['startDate']} to {payload['endDate']}",
        f"- Categories: {', '.join(payload['categories'])}",
        f"- Article count: {payload['articleCount']}",
        f"- Candidate count: {len(payload['candidates'])}",
        "",
        "## Ranked Candidates",
        "",
        "| Rank | Ticker | Name | Score | Mentions | Positive | Risk | Top queries |",
        "|---:|---|---|---:|---:|---:|---:|---|",
    ]
    for index, row in enumerate(payload["candidates"], start=1):
        top_queries = sorted(row["queries"].items(), key=lambda item: item[1], reverse=True)[:3]
        query_text = "<br>".join(f"{query} ({count})" for query, count in top_queries)
        lines.append(
            "| {rank} | {ticker} | {name} | {score} | {mentions} | {positive} | {risk} | {queries} |".format(
                rank=index,
                ticker=row["ticker"],
                name=row["name"],
                score=row["score"],
                mentions=row["mentionCount"],
                positive=row["positiveSignalCount"],
                risk=row["riskSignalCount"],
                queries=query_text,
            )
        )

    lines.extend(["", "## Evidence", ""])
    for row in payload["candidates"]:
        lines.extend([f"### {row['ticker']} {row['name']}", ""])
        for article in row["articles"]:
            lines.append(f"- [{article['category']}] {article['title']}")
            lines.append(f"  - Query: {article['query']}")
            if article.get("published_at"):
                lines.append(f"  - Published: {article['published_at']}")
            if article.get("link"):
                lines.append(f"  - Link: {article['link']}")
        lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = make_parser().parse_args()
    start_date = args.start_date or _default_start(args.end_date, args.days)
    categories = _split_csv(args.categories)
    extra_queries = _split_csv(args.extra_queries)
    set_config({"naver_news_display": args.display, "naver_news_sort": args.sort})

    articles = collect_candidate_news_naver(
        start_date,
        args.end_date,
        categories=categories,
        display=args.display,
        sort=args.sort,
        extra_queries=extra_queries,
    )
    candidates = rank_candidate_tickers(articles, limit=args.limit)
    payload = {
        "source": "Naver Search API",
        "purpose": "Korean stock candidate discovery",
        "startDate": start_date,
        "endDate": args.end_date,
        "categories": categories,
        "extraQueries": extra_queries,
        "articleCount": len(articles),
        "candidateCount": len(candidates),
        "candidates": candidates,
        "articles": articles,
    }

    prefix = _output_prefix(args.output_prefix)
    outputs: dict[str, str] = {}
    if args.format in {"json", "both"}:
        json_path = prefix.with_suffix(".json")
        _write_json(json_path, payload)
        outputs["json"] = str(json_path)
    if args.format in {"md", "both"}:
        md_path = prefix.with_suffix(".md")
        _write_markdown(md_path, payload)
        outputs["md"] = str(md_path)

    print(
        json.dumps(
            {
                "articleCount": len(articles),
                "candidateCount": len(candidates),
                "outputs": outputs,
                "topCandidates": candidates[:5],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

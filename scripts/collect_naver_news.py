from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.naver_news import collect_news_naver


PROJECT_DIR = Path(__file__).resolve().parents[1]
OUTPUT_DIR = PROJECT_DIR / ".local" / "news"


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Collect Korean stock news with Naver Search API.")
    parser.add_argument(
        "--tickers",
        default="005930.KS",
        help="Comma-separated tickers, e.g. 005930.KS,000660.KS,035720.KQ.",
    )
    parser.add_argument("--start-date", help="Start date YYYY-MM-DD. Default: end-date - days.")
    parser.add_argument("--end-date", default=datetime.now().strftime("%Y-%m-%d"), help="End date YYYY-MM-DD.")
    parser.add_argument("--days", type=int, default=7, help="Lookback days when --start-date is omitted.")
    parser.add_argument("--display", type=int, default=20, help="Naver display count per ticker, max 100.")
    parser.add_argument("--sort", choices=["date", "sim"], default="date", help="Naver news sort mode.")
    parser.add_argument("--format", choices=["json", "md"], default="md", help="Output format.")
    parser.add_argument("--output", help="Output file path. Default: .local/news/naver_news_<timestamp>.<format>.")
    return parser


def _parse_tickers(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _default_start(end_date: str, days: int) -> str:
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    return (end_dt - timedelta(days=days)).strftime("%Y-%m-%d")


def _output_path(fmt: str, explicit_path: str | None) -> Path:
    if explicit_path:
        return Path(explicit_path)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return OUTPUT_DIR / f"naver_news_{stamp}.{fmt}"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_markdown(path: Path, payload: dict[str, Any]) -> None:
    lines = [
        "# Naver Stock News",
        "",
        f"- Period: {payload['startDate']} to {payload['endDate']}",
        f"- Tickers: {', '.join(payload['tickers'])}",
        f"- Article count: {len(payload['articles'])}",
        "",
    ]
    by_ticker: dict[str, list[dict[str, Any]]] = {}
    for article in payload["articles"]:
        by_ticker.setdefault(article["ticker"], []).append(article)

    for ticker, articles in by_ticker.items():
        lines.extend([f"## {ticker}", ""])
        if not articles:
            lines.extend(["No articles found.", ""])
            continue
        for index, article in enumerate(articles, start=1):
            lines.append(f"### {index}. {article['title']}")
            lines.append(f"- Query: {article['query']}")
            lines.append(f"- Published: {article['published_at'] or 'unknown'}")
            if article["description"]:
                lines.append(f"- Summary: {article['description']}")
            if article["link"]:
                lines.append(f"- Link: {article['link']}")
            lines.append("")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> int:
    args = make_parser().parse_args()
    tickers = _parse_tickers(args.tickers)
    start_date = args.start_date or _default_start(args.end_date, args.days)
    set_config({"naver_news_display": args.display, "naver_news_sort": args.sort})

    articles: list[dict[str, Any]] = []
    for ticker in tickers:
        articles.extend(
            collect_news_naver(
                ticker,
                start_date,
                args.end_date,
                display=args.display,
                sort=args.sort,
            )
        )

    payload = {
        "source": "Naver Search API",
        "startDate": start_date,
        "endDate": args.end_date,
        "tickers": tickers,
        "articleCount": len(articles),
        "articles": articles,
    }
    output_path = _output_path(args.format, args.output)
    if args.format == "json":
        _write_json(output_path, payload)
    else:
        _write_markdown(output_path, payload)

    print(json.dumps({"articleCount": len(articles), "outputPath": str(output_path)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

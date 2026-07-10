"""Naver Search API news vendor for Korean-market analysis."""

from __future__ import annotations

import html
import json
import os
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import requests
from dateutil.relativedelta import relativedelta

from .config import get_config
from .errors import VendorNotConfiguredError, VendorRateLimitError

NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"

DEFAULT_KOREAN_QUERY_MAP = {
    "005930": "삼성전자",
    "000660": "SK하이닉스",
    "035420": "NAVER",
    "035720": "카카오",
    "005380": "현대차",
    "000270": "기아",
    "068270": "셀트리온",
    "207940": "삼성바이오로직스",
    "373220": "LG에너지솔루션",
    "105560": "KB금융",
    "055550": "신한지주",
}


def _strip_html(value: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", value)).strip()


def _parse_pub_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return parsedate_to_datetime(value).replace(tzinfo=None)
    except (TypeError, ValueError):
        return None


def _base_symbol(symbol: str) -> str:
    return symbol.strip().upper().split(".", 1)[0]


def _query_for_symbol(symbol: str) -> str:
    config = get_config()
    base = _base_symbol(symbol)
    query_map = {
        **DEFAULT_KOREAN_QUERY_MAP,
        **config.get("naver_news_query_map", {}),
    }
    return query_map.get(symbol.upper()) or query_map.get(base) or base


def _credentials() -> tuple[str, str]:
    client_id = os.environ.get("NAVER_CLIENT_ID")
    client_secret = os.environ.get("NAVER_CLIENT_SECRET")
    if not client_id or not client_secret:
        raise VendorNotConfiguredError(
            "Naver news vendor requires NAVER_CLIENT_ID and NAVER_CLIENT_SECRET."
        )
    return client_id, client_secret


def _search(query: str, display: int, sort: str) -> list[dict[str, Any]]:
    client_id, client_secret = _credentials()
    response = requests.get(
        NAVER_NEWS_URL,
        params={"query": query, "display": display, "start": 1, "sort": sort},
        headers={
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        },
        timeout=10,
    )
    if response.status_code == 429:
        raise VendorRateLimitError("Naver Search API rate limit reached.")
    if response.status_code >= 400:
        raise RuntimeError(f"Naver Search API error {response.status_code}: {response.text}")
    return response.json().get("items", [])


def _format_articles(
    title: str,
    articles: list[dict[str, Any]],
    start_dt: datetime,
    end_dt: datetime,
) -> str:
    lines = [f"## {title}\n"]
    kept = 0
    for raw in articles:
        pub_dt = _parse_pub_date(raw.get("pubDate"))
        if pub_dt is not None and not (start_dt <= pub_dt <= end_dt + relativedelta(days=1)):
            continue
        article_title = _strip_html(str(raw.get("title", "No title")))
        description = _strip_html(str(raw.get("description", "")))
        link = raw.get("originallink") or raw.get("link") or ""
        source_date = pub_dt.strftime("%Y-%m-%d %H:%M") if pub_dt else "unknown date"
        lines.append(f"### {article_title} (source: Naver, {source_date})")
        if description:
            lines.append(description)
        if link:
            lines.append(f"Link: {link}")
        lines.append("")
        kept += 1

    if kept == 0:
        return f"No Naver news found for {title} in the requested date window."
    return "\n".join(lines)


def _parse_collected_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=None)
    except ValueError:
        return None


def _load_collected_articles(path_value: str | None) -> list[dict[str, Any]]:
    if not path_value:
        return []
    path = Path(path_value).expanduser()
    if not path.exists() or path.suffix.lower() != ".json":
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        articles = payload.get("articles", [])
        return articles if isinstance(articles, list) else []
    if isinstance(payload, list):
        return payload
    return []


def _collected_articles_for(
    ticker: str,
    start_date: str,
    end_date: str,
    *,
    path_value: str | None,
) -> list[dict[str, Any]]:
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    base = _base_symbol(ticker)
    rows: list[dict[str, Any]] = []
    for article in _load_collected_articles(path_value):
        article_ticker = str(article.get("ticker", ""))
        if _base_symbol(article_ticker) != base:
            continue
        pub_dt = _parse_collected_date(article.get("published_at"))
        if pub_dt is not None and not (start_dt <= pub_dt <= end_dt + relativedelta(days=1)):
            continue
        rows.append(article)
    return rows


def _format_collected_articles(
    title: str,
    articles: list[dict[str, Any]],
) -> str:
    if not articles:
        return ""
    lines = [f"## {title}", "", "Source mode: pre-collected Naver Search API snapshot.", ""]
    for raw in articles:
        article_title = str(raw.get("title", "No title")).strip()
        description = str(raw.get("description", "")).strip()
        link = str(raw.get("link", "")).strip()
        source_date = raw.get("published_at") or "unknown date"
        query = raw.get("query")
        query_suffix = f", query: {query}" if query else ""
        lines.append(f"### {article_title} (source: Naver, {source_date}{query_suffix})")
        if description:
            lines.append(description)
        if link:
            lines.append(f"Link: {link}")
        lines.append("")
    return "\n".join(lines)


def collect_news_naver(
    ticker: str,
    start_date: str,
    end_date: str,
    *,
    display: int | None = None,
    sort: str | None = None,
) -> list[dict[str, Any]]:
    """Collect structured Naver news articles for a Korean stock ticker.

    The result is intended for local export and downstream processing. It keeps
    the same query mapping and date-window filtering used by the TradingAgents
    news analyst path.
    """
    config = get_config()
    resolved_display = int(
        display if display is not None else config.get("naver_news_display", config.get("news_article_limit", 10))
    )
    resolved_sort = sort or config.get("naver_news_sort", "date")
    query = _query_for_symbol(ticker)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    for raw in _search(query, display=resolved_display, sort=resolved_sort):
        pub_dt = _parse_pub_date(raw.get("pubDate"))
        if pub_dt is not None and not (start_dt <= pub_dt <= end_dt + relativedelta(days=1)):
            continue
        rows.append(
            {
                "ticker": ticker,
                "query": query,
                "title": _strip_html(str(raw.get("title", "No title"))),
                "description": _strip_html(str(raw.get("description", ""))),
                "link": raw.get("originallink") or raw.get("link") or "",
                "naver_link": raw.get("link") or "",
                "published_at": pub_dt.isoformat(timespec="minutes") if pub_dt else None,
                "source": "Naver Search API",
            }
        )
    return rows


def get_news_naver(ticker: str, start_date: str, end_date: str) -> str:
    config = get_config()
    if config.get("naver_news_use_collected", True):
        collected = _collected_articles_for(
            ticker,
            start_date,
            end_date,
            path_value=config.get("naver_news_collected_data_path"),
        )
        if collected:
            return _format_collected_articles(
                f"Naver News for {ticker}, from {start_date} to {end_date}:",
                collected,
            )
        if config.get("naver_news_collected_data_path") and not config.get("naver_news_live_fallback", True):
            return f"No collected Naver news found for {ticker} between {start_date} and {end_date}."

    display = int(config.get("naver_news_display", config.get("news_article_limit", 10)))
    sort = config.get("naver_news_sort", "date")
    query = _query_for_symbol(ticker)
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    articles = _search(query, display=display, sort=sort)
    return _format_articles(
        f"Naver News for {ticker} (query: {query}), from {start_date} to {end_date}:",
        articles,
        start_dt,
        end_dt,
    )


def get_global_news_naver(
    curr_date: str,
    look_back_days: int | None = None,
    limit: int | None = None,
) -> str:
    config = get_config()
    if look_back_days is None:
        look_back_days = int(config["global_news_lookback_days"])
    if limit is None:
        limit = int(config.get("global_news_article_limit", 10))
    queries = config.get("naver_global_news_queries") or [
        "코스피 환율 금리 증시",
        "한국은행 금리 물가 경기",
        "미국 연준 금리 나스닥 반도체",
    ]
    curr_dt = datetime.strptime(curr_date, "%Y-%m-%d")
    start_dt = curr_dt - relativedelta(days=look_back_days)
    articles: list[dict[str, Any]] = []
    seen = set()
    for query in queries:
        for item in _search(query, display=limit, sort="date"):
            key = item.get("originallink") or item.get("link") or item.get("title")
            if key and key not in seen:
                seen.add(key)
                articles.append(item)
            if len(articles) >= limit:
                break
        if len(articles) >= limit:
            break
    return _format_articles(
        f"Naver Global/Korea Market News, from {start_dt:%Y-%m-%d} to {curr_date}:",
        articles,
        start_dt,
        curr_dt,
    )

"""Naver Search API news vendor for Korean-market analysis."""

from __future__ import annotations

import html
import os
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
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


def get_news_naver(ticker: str, start_date: str, end_date: str) -> str:
    config = get_config()
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

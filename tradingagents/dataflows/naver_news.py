"""Naver Search API news vendor for Korean-market analysis."""

from __future__ import annotations

import html
import json
import os
import re
import time
from contextlib import contextmanager
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

import fcntl

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

DEFAULT_CANDIDATE_SYMBOL_MAP = {
    **DEFAULT_KOREAN_QUERY_MAP,
    "012330": "현대모비스",
    "028260": "삼성물산",
    "006400": "삼성SDI",
    "051910": "LG화학",
    "096770": "SK이노베이션",
    "003670": "포스코퓨처엠",
    "247540": "에코프로비엠",
    "086520": "에코프로",
    "010140": "삼성중공업",
    "329180": "HD현대중공업",
    "042660": "한화오션",
    "009540": "HD한국조선해양",
    "047810": "한국항공우주",
    "012450": "한화에어로스페이스",
    "079550": "LIG넥스원",
    "010120": "LS ELECTRIC",
    "000880": "한화",
    "009830": "한화솔루션",
    "034020": "두산에너빌리티",
    "267260": "HD현대일렉트릭",
    "298040": "효성중공업",
    "064350": "현대로템",
    "066570": "LG전자",
    "011070": "LG이노텍",
    "009150": "삼성전기",
    "042700": "한미반도체",
    "000990": "DB하이텍",
    "039030": "이오테크닉스",
    "240810": "원익IPS",
    "403870": "HPSP",
    "086790": "하나금융지주",
    "316140": "우리금융지주",
    "323410": "카카오뱅크",
    "033780": "KT&G",
    "090430": "아모레퍼시픽",
    "051900": "LG생활건강",
    "352820": "하이브",
    "251270": "넷마블",
    "259960": "크래프톤",
    "263750": "펄어비스",
}

DEFAULT_CANDIDATE_KEYWORDS = {
    "market": [
        "코스피 외국인 기관 수급",
        "코스닥 외국인 기관 수급",
        "원달러 환율 한국 증시",
        "미국 금리 나스닥 반도체",
        "필라델피아 반도체 지수",
    ],
    "themes": [
        "AI 반도체 HBM",
        "전력기기 변압기 전력망",
        "방산 수출 K방산",
        "조선 LNG선 수주",
        "바이오시밀러 ADC 항암제",
        "2차전지 전고체 배터리",
        "밸류업 저PBR 주주환원",
    ],
    "selection_signals": [
        "실적 서프라이즈 목표주가 상향",
        "외국인 순매수 기관 순매수",
        "신규 수주 공급 계약",
        "자사주 소각 배당 확대",
        "52주 신고가 거래대금 증가",
    ],
    "risk_signals": [
        "실적 쇼크 목표주가 하향",
        "유상증자 전환사채 CB",
        "거래정지 불성실공시",
        "소송 제재 리콜",
        "수요 둔화 공급 과잉",
    ],
}

_CANDIDATE_CATEGORY_WEIGHTS = {
    "selection_signals": 3,
    "themes": 2,
    "market": 1,
    "risk_signals": -2,
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


def _quota_state_path() -> Path:
    config = get_config()
    configured = str(config.get("naver_rate_limit_state_path") or "").strip()
    if configured:
        return Path(configured).expanduser()
    cache_dir = Path(config.get("data_cache_dir") or Path.home() / ".tradingagents" / "cache")
    return cache_dir / "naver_search_quota.json"


@contextmanager
def _locked_quota_state(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            if path.exists():
                try:
                    state = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    state = {}
            else:
                state = {}
            yield state
            tmp_path = path.with_suffix(path.suffix + ".tmp")
            tmp_path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            tmp_path.replace(path)
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _reserve_quota_slot() -> None:
    config = get_config()
    if not config.get("naver_rate_limit_enabled", True):
        return

    per_second_limit = int(config.get("naver_rate_limit_per_second", 9))
    daily_limit = int(config.get("naver_rate_limit_daily", 24000))
    if per_second_limit <= 0 and daily_limit <= 0:
        return

    path = _quota_state_path()
    while True:
        now = time.time()
        current_second = str(int(now))
        today = datetime.now().strftime("%Y-%m-%d")
        wait_seconds = 0.0
        with _locked_quota_state(path) as state:
            if state.get("date") != today:
                state.clear()
                state["date"] = today
                state["daily_count"] = 0
                state["seconds"] = {}

            seconds = state.setdefault("seconds", {})
            # Keep only the current and previous second for easy inspection
            # while avoiding unbounded ledger growth.
            previous_second = str(int(now) - 1)
            state["seconds"] = {
                key: value
                for key, value in seconds.items()
                if key in {previous_second, current_second}
            }
            seconds = state["seconds"]

            daily_count = int(state.get("daily_count", 0))
            if daily_limit > 0 and daily_count >= daily_limit:
                raise VendorRateLimitError(
                    f"Naver Search API daily quota exhausted: {daily_count}/{daily_limit}."
                )

            second_count = int(seconds.get(current_second, 0))
            if per_second_limit <= 0 or second_count < per_second_limit:
                seconds[current_second] = second_count + 1
                state["daily_count"] = daily_count + 1
                state["updated_at"] = datetime.now().isoformat(timespec="seconds")
                return

            wait_seconds = max(0.05, int(now) + 1 - now + 0.05)

        time.sleep(wait_seconds)


def _search_once(query: str, display: int, sort: str) -> requests.Response:
    client_id, client_secret = _credentials()
    _reserve_quota_slot()
    return requests.get(
        NAVER_NEWS_URL,
        params={"query": query, "display": display, "start": 1, "sort": sort},
        headers={
            "X-Naver-Client-Id": client_id,
            "X-Naver-Client-Secret": client_secret,
        },
        timeout=10,
    )


def _search(query: str, display: int, sort: str) -> list[dict[str, Any]]:
    config = get_config()
    retries = int(config.get("naver_rate_limit_retries", 2))
    backoff = float(config.get("naver_rate_limit_backoff_seconds", 1.0))
    response = None
    for attempt in range(retries + 1):
        response = _search_once(query, display, sort)
        if response.status_code != 429:
            break
        if attempt >= retries:
            raise VendorRateLimitError("Naver Search API rate limit reached.")
        retry_after = response.headers.get("Retry-After")
        try:
            wait_seconds = float(retry_after) if retry_after else backoff * (2 ** attempt)
        except ValueError:
            wait_seconds = backoff * (2 ** attempt)
        time.sleep(max(wait_seconds, 0.1))

    if response is None:
        raise RuntimeError("Naver Search API request was not attempted.")
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


def _candidate_keywords() -> dict[str, list[str]]:
    config = get_config()
    configured = config.get("naver_candidate_keywords") or {}
    if not isinstance(configured, dict):
        return DEFAULT_CANDIDATE_KEYWORDS
    merged = {key: list(value) for key, value in DEFAULT_CANDIDATE_KEYWORDS.items()}
    for category, queries in configured.items():
        if isinstance(queries, str):
            merged[category] = [queries]
        elif isinstance(queries, list):
            merged[category] = [str(query) for query in queries if str(query).strip()]
    return merged


def _candidate_symbol_map() -> dict[str, str]:
    config = get_config()
    return {
        **DEFAULT_CANDIDATE_SYMBOL_MAP,
        **config.get("naver_news_query_map", {}),
    }


def collect_keyword_news_naver(
    query: str,
    start_date: str,
    end_date: str,
    *,
    category: str = "custom",
    display: int | None = None,
    sort: str | None = None,
) -> list[dict[str, Any]]:
    """Collect structured Naver news rows for one discovery keyword."""
    config = get_config()
    resolved_display = int(
        display if display is not None else config.get("naver_news_display", config.get("news_article_limit", 10))
    )
    resolved_sort = sort or config.get("naver_news_sort", "date")
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    rows: list[dict[str, Any]] = []
    for raw in _search(query, display=resolved_display, sort=resolved_sort):
        pub_dt = _parse_pub_date(raw.get("pubDate"))
        if pub_dt is not None and not (start_dt <= pub_dt <= end_dt + relativedelta(days=1)):
            continue
        rows.append(
            {
                "category": category,
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


def collect_candidate_news_naver(
    start_date: str,
    end_date: str,
    *,
    categories: list[str] | None = None,
    display: int | None = None,
    sort: str | None = None,
    extra_queries: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Collect market/theme/signal news used to discover candidate stocks."""
    keyword_map = _candidate_keywords()
    selected_categories = categories or list(keyword_map)
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add_rows(query: str, category: str) -> None:
        for row in collect_keyword_news_naver(
            query,
            start_date,
            end_date,
            category=category,
            display=display,
            sort=sort,
        ):
            key = (row.get("link") or row.get("naver_link") or row.get("title", ""), row["query"])
            if key in seen:
                continue
            seen.add(key)
            rows.append(row)

    for category in selected_categories:
        for query in keyword_map.get(category, []):
            add_rows(query, category)
    for query in extra_queries or []:
        if query.strip():
            add_rows(query.strip(), "custom")
    return rows


def rank_candidate_tickers(
    articles: list[dict[str, Any]],
    *,
    symbol_map: dict[str, str] | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Rank known Korean tickers mentioned in discovery news rows."""
    candidates = symbol_map or _candidate_symbol_map()
    scores: dict[str, dict[str, Any]] = {}
    for article in articles:
        text = f"{article.get('title', '')} {article.get('description', '')}"
        category = str(article.get("category", "custom"))
        weight = _CANDIDATE_CATEGORY_WEIGHTS.get(category, 1)
        for code, name in candidates.items():
            base = _base_symbol(code)
            aliases = {base, str(name)}
            if str(name).upper() == "NAVER":
                aliases.add("네이버")
            if not any(alias and alias in text for alias in aliases):
                continue
            ticker = f"{base}.KS" if base.isdigit() else code
            row = scores.setdefault(
                ticker,
                {
                    "ticker": ticker,
                    "name": name,
                    "score": 0,
                    "mentionCount": 0,
                    "positiveSignalCount": 0,
                    "riskSignalCount": 0,
                    "categories": {},
                    "queries": {},
                    "articles": [],
                },
            )
            row["score"] += weight
            row["mentionCount"] += 1
            if category == "selection_signals":
                row["positiveSignalCount"] += 1
            if category == "risk_signals":
                row["riskSignalCount"] += 1
            row["categories"][category] = row["categories"].get(category, 0) + 1
            query = str(article.get("query", ""))
            row["queries"][query] = row["queries"].get(query, 0) + 1
            if len(row["articles"]) < 5:
                row["articles"].append(
                    {
                        "title": article.get("title"),
                        "query": article.get("query"),
                        "category": category,
                        "published_at": article.get("published_at"),
                        "link": article.get("link"),
                    }
                )

    ranked = sorted(
        scores.values(),
        key=lambda row: (
            row["score"],
            row["positiveSignalCount"],
            row["mentionCount"],
            -row["riskSignalCount"],
            row["ticker"],
        ),
        reverse=True,
    )
    return ranked[:limit]


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

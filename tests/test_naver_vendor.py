from __future__ import annotations

from unittest import mock

from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows import naver_news
from datetime import datetime
import json


def test_naver_registered_as_news_vendor():
    assert "naver" in interface.VENDOR_LIST
    assert "naver" in interface.VENDOR_METHODS["get_news"]
    assert "naver" in interface.VENDOR_METHODS["get_global_news"]


def test_naver_excludes_next_day_midnight_from_live_and_saved_news(tmp_path):
    raw = [{"title": "Future item", "pubDate": "Fri, 11 Sep 2026 00:00:00 +0900"}]
    with mock.patch.object(naver_news, "_search", return_value=raw):
        assert naver_news.collect_news_naver("005930.KS", "2026-09-09", "2026-09-10") == []
    rendered = naver_news._format_articles("news", raw, datetime(2026, 9, 9), datetime(2026, 9, 10))
    assert "Future item" not in rendered
    path = tmp_path / "news.json"
    path.write_text(json.dumps({"articles": [{"ticker": "005930.KS", "published_at": "2026-09-11T00:00", "title": "Future item"}]}))
    assert naver_news._collected_articles_for("005930.KS", "2026-09-09", "2026-09-10", path_value=str(path)) == []


def test_corrupt_naver_snapshot_does_not_break_live_fallback(tmp_path):
    path = tmp_path / "broken.json"
    path.write_text("{broken")
    assert naver_news._load_collected_articles(str(path)) == []


def test_route_to_naver_news_vendor():
    set_config({"data_vendors": {"news_data": "naver"}})
    impl = mock.Mock(return_value="NAVER_NEWS")
    with mock.patch.dict(interface.VENDOR_METHODS["get_news"], {"naver": impl}, clear=True):
        result = interface.route_to_vendor("get_news", "005930.KS", "2026-07-01", "2026-07-09")
    assert result == "NAVER_NEWS"
    impl.assert_called_once_with("005930.KS", "2026-07-01", "2026-07-09")


def test_collect_news_naver_returns_structured_articles():
    set_config({"naver_news_display": 10, "naver_news_sort": "date"})
    raw = [
        {
            "title": "<b>삼성전자</b> 실적 개선",
            "description": "반도체 업황 회복",
            "originallink": "https://example.com/a",
            "link": "https://naver.example/a",
            "pubDate": "Fri, 10 Jul 2026 09:30:00 +0900",
        }
    ]
    with mock.patch.object(naver_news, "_search", return_value=raw) as search:
        rows = naver_news.collect_news_naver("005930.KS", "2026-07-09", "2026-07-10")

    assert rows == [
        {
            "ticker": "005930.KS",
            "query": "삼성전자",
            "title": "삼성전자 실적 개선",
            "description": "반도체 업황 회복",
            "link": "https://example.com/a",
            "naver_link": "https://naver.example/a",
            "published_at": "2026-07-10T09:30",
            "source": "Naver Search API",
        }
    ]
    search.assert_called_once_with("삼성전자", display=10, sort="date")


def test_get_news_naver_uses_collected_snapshot(tmp_path):
    path = tmp_path / "naver_news.json"
    path.write_text(
        """
{
  "articles": [
    {
      "ticker": "005930.KS",
      "query": "삼성전자",
      "title": "수집된 삼성전자 뉴스",
      "description": "분석 전에 저장된 기사",
      "link": "https://example.com/collected",
      "published_at": "2026-07-10T09:30",
      "source": "Naver Search API"
    }
  ]
}
""",
        encoding="utf-8",
    )
    set_config(
        {
            "naver_news_collected_data_path": str(path),
            "naver_news_use_collected": True,
            "naver_news_live_fallback": False,
        }
    )
    with mock.patch.object(naver_news, "_search") as search:
        result = naver_news.get_news_naver("005930.KS", "2026-07-09", "2026-07-10")

    assert "Source mode: pre-collected Naver Search API snapshot" in result
    assert "수집된 삼성전자 뉴스" in result
    search.assert_not_called()


def test_collect_keyword_news_naver_tags_category_and_query():
    raw = [
        {
            "title": "AI 반도체 HBM 수요 확대",
            "description": "삼성전자와 SK하이닉스가 주목받았다",
            "originallink": "https://example.com/hbm",
            "link": "https://naver.example/hbm",
            "pubDate": "Fri, 10 Jul 2026 09:30:00 +0900",
        }
    ]
    with mock.patch.object(naver_news, "_search", return_value=raw) as search:
        rows = naver_news.collect_keyword_news_naver(
            "AI 반도체 HBM",
            "2026-07-09",
            "2026-07-10",
            category="themes",
            display=5,
        )

    assert rows == [
        {
            "category": "themes",
            "query": "AI 반도체 HBM",
            "title": "AI 반도체 HBM 수요 확대",
            "description": "삼성전자와 SK하이닉스가 주목받았다",
            "link": "https://example.com/hbm",
            "naver_link": "https://naver.example/hbm",
            "published_at": "2026-07-10T09:30",
            "source": "Naver Search API",
        }
    ]
    search.assert_called_once_with("AI 반도체 HBM", display=5, sort="date")


def test_rank_candidate_tickers_scores_positive_and_risk_mentions():
    articles = [
        {
            "category": "selection_signals",
            "query": "실적 서프라이즈 목표주가 상향",
            "title": "삼성전자 목표주가 상향",
            "description": "HBM 실적 개선 기대",
            "link": "https://example.com/a",
            "published_at": "2026-07-10T09:30",
        },
        {
            "category": "risk_signals",
            "query": "실적 쇼크 목표주가 하향",
            "title": "SK하이닉스 공급 과잉 우려",
            "description": "반도체 수요 둔화",
            "link": "https://example.com/b",
            "published_at": "2026-07-10T10:00",
        },
        {
            "category": "themes",
            "query": "AI 반도체 HBM",
            "title": "삼성전자 SK하이닉스 HBM 경쟁",
            "description": "",
            "link": "https://example.com/c",
            "published_at": "2026-07-10T11:00",
        },
    ]

    ranked = naver_news.rank_candidate_tickers(articles, limit=10)

    samsung = next(row for row in ranked if row["ticker"] == "005930.KS")
    hynix = next(row for row in ranked if row["ticker"] == "000660.KS")
    assert samsung["score"] == 5
    assert samsung["positiveSignalCount"] == 1
    assert hynix["score"] == 0
    assert hynix["riskSignalCount"] == 1
    assert ranked[0]["ticker"] == "005930.KS"


def test_candidate_news_is_deduplicated_across_queries_with_risk_precedence():
    base = {'title': '삼성전자', 'link': 'https://example.com/same', 'published_at': '2026-10-22T09:00'}
    articles = [{**base, 'category': 'selection_signals', 'query': 'one'},
                {**base, 'category': 'themes', 'query': 'two'}]
    rows = naver_news.rank_candidate_tickers(articles)
    assert rows[0]['mentionCount'] == 1 and rows[0]['score'] == 3
    articles.append({**base, 'category': 'risk_signals', 'query': 'three'})
    rows = naver_news.rank_candidate_tickers(articles)
    assert rows[0]['mentionCount'] == 1 and rows[0]['score'] == -2


def test_candidate_news_freshness_and_future_dates():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    as_of = datetime(2026, 10, 22, 10, tzinfo=ZoneInfo('Asia/Seoul'))
    articles = [{'title': '삼성전자', 'category': 'selection_signals', 'link': str(i), 'published_at': at}
                for i, at in enumerate(['2026-10-22T09:00', '2026-10-21T09:00', '2026-10-20T09:00',
                                        '2026-10-22T11:00', None])]
    row = naver_news.rank_candidate_tickers(articles, as_of=as_of)[0]
    assert row['score'] == 3 + 1.5 + .75 + .75 and row['mentionCount'] == 4


def test_naver_quota_ledger_blocks_before_exceeding_second_limit(tmp_path, monkeypatch):
    state_path = tmp_path / "quota.json"
    set_config(
        {
            "naver_rate_limit_enabled": True,
            "naver_rate_limit_per_second": 1,
            "naver_rate_limit_daily": 10,
            "naver_rate_limit_state_path": str(state_path),
        }
    )
    times = iter([1000.10, 1000.20, 1001.05])
    sleeps = []
    monkeypatch.setattr(naver_news.time, "time", lambda: next(times))
    monkeypatch.setattr(naver_news.time, "sleep", lambda value: sleeps.append(value))

    naver_news._reserve_quota_slot()
    naver_news._reserve_quota_slot()

    assert sleeps and sleeps[0] > 0
    payload = state_path.read_text(encoding="utf-8")
    assert '"daily_count": 2' in payload


def test_search_retries_once_after_429(monkeypatch, tmp_path):
    set_config(
        {
            "naver_rate_limit_enabled": False,
            "naver_rate_limit_retries": 1,
            "naver_rate_limit_backoff_seconds": 0.01,
            "naver_rate_limit_state_path": str(tmp_path / "quota.json"),
        }
    )

    class Response:
        def __init__(self, status_code):
            self.status_code = status_code
            self.text = ""
            self.headers = {}

        def json(self):
            return {"items": [{"title": "ok"}]}

    responses = [Response(429), Response(200)]
    monkeypatch.setenv("NAVER_CLIENT_ID", "id")
    monkeypatch.setenv("NAVER_CLIENT_SECRET", "secret")
    monkeypatch.setattr(naver_news.time, "sleep", lambda value: None)
    monkeypatch.setattr(naver_news.requests, "get", lambda *args, **kwargs: responses.pop(0))

    rows = naver_news._search("삼성전자", display=1, sort="date")

    assert rows == [{"title": "ok"}]
    assert responses == []

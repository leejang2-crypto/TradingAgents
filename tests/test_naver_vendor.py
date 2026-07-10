from __future__ import annotations

from unittest import mock

from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows import naver_news


def test_naver_registered_as_news_vendor():
    assert "naver" in interface.VENDOR_LIST
    assert "naver" in interface.VENDOR_METHODS["get_news"]
    assert "naver" in interface.VENDOR_METHODS["get_global_news"]


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

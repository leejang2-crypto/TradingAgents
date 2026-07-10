from __future__ import annotations

from unittest import mock

from tradingagents.dataflows import interface
from tradingagents.dataflows.config import set_config


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

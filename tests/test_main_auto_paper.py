from __future__ import annotations

import argparse
from datetime import datetime
from decimal import Decimal

import main


def test_order_budget_defaults_to_ten_percent_of_paper_capital():
    args = argparse.Namespace(max_order_amount=None, paper_capital="10000000", position_pct="0.10")
    assert main.order_budget(args) == Decimal("1000000")


def test_daily_profit_target_defaults_to_one_percent_of_paper_capital():
    args = argparse.Namespace(paper_capital="10000000", daily_target_return="0.01")
    assert main.daily_profit_target(args) == Decimal("100000")


def test_collect_candidate_snapshot_degrades_on_discovery_error(monkeypatch, tmp_path):
    def boom(*args, **kwargs):
        raise RuntimeError("rate limited")

    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "collect_candidate_news_naver", boom)

    args = argparse.Namespace(
        date="2026-07-13",
        candidate_days=3,
        candidate_categories="market,themes",
        extra_queries="",
        news_display=1,
        candidate_limit=5,
        paper_capital="10000000",
        position_pct="0.10",
        daily_target_return="0.01",
        trading_style="intraday-scalping",
        max_order_amount=None,
    )

    snapshot = main.collect_candidate_snapshot(args)

    assert snapshot["ok"] is False
    assert snapshot["error"] == "rate limited"
    assert snapshot["candidates"] == []
    assert snapshot["articleCount"] == 0
    assert (tmp_path / "candidates").exists()


def test_make_config_forwards_llm_timeout():
    args = argparse.Namespace(
        provider="openai",
        quick_model="gpt-5.5",
        deep_model="gpt-5.5",
        model=None,
        backend_url=None,
        language="Korean",
        llm_timeout="180",
        paper_capital="10000000",
        position_pct="0.10",
        daily_target_return="0.01",
        trading_style="intraday-scalping",
        max_order_amount=None,
    )

    config = main.make_config(args, korea_mode=False, minimal_mode=True)

    assert config["llm_timeout"] == "180"
    assert config["llm_provider"] == "openai"
    assert config["trading_objective"]["capital_krw"] == "10000000"
    assert config["trading_objective"]["daily_target_profit_krw"] == "100000"


def test_regular_market_hours_are_korea_weekday_only():
    in_session = datetime(2026, 9, 3, 9, 30, tzinfo=main.KOREA_TZ)
    after_close = datetime(2026, 9, 3, 15, 31, tzinfo=main.KOREA_TZ)
    weekend = datetime(2026, 9, 5, 10, 0, tzinfo=main.KOREA_TZ)

    assert main.is_regular_market_open(in_session)
    assert not main.is_regular_market_open(after_close)
    assert not main.is_regular_market_open(weekend)


def test_next_regular_market_open_skips_weekends():
    friday_after_close = datetime(2026, 9, 4, 16, 0, tzinfo=main.KOREA_TZ)

    next_open = main.next_regular_market_open(friday_after_close)

    assert next_open.isoformat() == "2026-09-07T09:00:00+09:00"


def test_next_regular_market_open_skips_configured_holidays():
    friday_after_close = datetime(2026, 9, 4, 16, 0, tzinfo=main.KOREA_TZ)
    holidays = main.holiday_dates("2026-09-07")

    next_open = main.next_regular_market_open(friday_after_close, holidays=holidays)

    assert next_open.isoformat() == "2026-09-08T09:00:00+09:00"

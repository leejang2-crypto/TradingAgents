from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
from datetime import datetime
from decimal import Decimal

import main
import pytest


def test_latest_analyses_show_saved_names_and_ticker_matched_fallback():
    page = main.render_monitor_html({
        "latestCandidates": {"candidates": [
            {"ticker": "000660", "name": "SK하이닉스"},
            {"ticker": "005930", "name": "다른 이름"},
        ]},
        "latestSummary": {"analyses": [
            {"ticker": "005930", "name": "삼성전자", "candidateRank": 1},
            {"ticker": "000660", "candidateRank": 2},
            {"ticker": "999999", "candidateRank": 1},
            {"ticker": "123456", "name": "<script>alert(1)</script>"},
        ]},
    })
    section = page.split('<section data-live="analyses">', 1)[1].split('</section>', 1)[0]
    assert '<th>Ticker</th><th>종목명</th><th>Rank</th>' in section
    assert '<td>005930</td><td>삼성전자</td><td>1</td>' in section
    assert '<td>000660</td><td>SK하이닉스</td><td>2</td>' in section
    assert '<td>999999</td><td>-</td>' in section
    assert '&lt;script&gt;alert(1)&lt;/script&gt;' in section
    assert '<script>' not in section


def test_latest_analyses_empty_row_spans_name_column():
    page = main.render_monitor_html({})
    assert '<td colspan="7">No analysis yet</td>' in page


@pytest.mark.parametrize("fails", [False, True])
def test_auto_trading_persists_candidate_name_even_on_error(monkeypatch, tmp_path, fails):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "collect_candidate_snapshot", lambda args: {
        "candidates": [{"ticker": "005930", "name": "삼성전자"}],
    })

    async def analyze(args, *, execute):
        if fails:
            raise RuntimeError("analysis failed")
        return {"ticker": args.korea_ticker, "ok": True}

    monkeypatch.setattr(main, "run_korea_toss", analyze)
    args = main.make_parser().parse_args(["--run-mode", "once", "--date", "2026-09-15"])
    result = asyncio.run(main.run_auto_trading(args, execute=False))
    saved = json.loads(main.Path(result["summaryPath"]).read_text())
    assert saved["analyses"][0]["name"] == "삼성전자"
    assert saved["analyses"][0]["ok"] is not fails


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
    assert config["trading_objective"]["position_target_return"] == "0.01"
    assert "daily_target_profit_krw" not in config["trading_objective"]


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


def test_monitor_snapshot_degrades_without_artifacts(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)

    snapshot = main.build_monitor_snapshot()

    assert snapshot["timezone"] == "Asia/Seoul"
    assert snapshot["latestSummaryPath"] is None
    assert snapshot["recentTrades"] == []


def test_monitor_status_file_round_trips(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)

    main.write_monitor_status("waiting", nextRunAfter="2026-09-04T09:00:00+09:00")
    snapshot = main.build_monitor_snapshot()

    assert snapshot["runtime"]["status"] == "waiting"
    assert snapshot["runtime"]["nextRunAfter"] == "2026-09-04T09:00:00+09:00"


def test_trading_settings_save_and_apply(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    args = argparse.Namespace(
        paper_capital="10000000",
        position_pct="0.10",
        daily_target_return="0.01",
        trading_style="intraday-scalping",
        candidate_limit=20,
        trade_candidates=3,
        candidate_days=3,
        candidate_categories="market,themes",
        extra_queries="",
        news_display=5,
        market_open="09:00",
        market_close="15:30",
        holiday_dates="",
        llm_timeout="180",
    )

    main.initialize_trading_settings(args)
    saved = main.save_trading_settings({"paper_capital": "12000000", "trade_candidates": "4"})
    main.apply_trading_settings(args)

    assert saved["paper_capital"] == "12000000"
    assert saved["trade_candidates"] == 4
    assert args.paper_capital == "12000000"
    assert args.trade_candidates == 4


def test_trading_settings_reject_invalid_position_pct():
    try:
        main.validate_trading_settings({"position_pct": "1.5"})
    except ValueError as exc:
        assert "position_pct" in str(exc)
    else:
        raise AssertionError("Expected ValueError")


def test_monitor_token_is_created_once(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)

    first = main.ensure_monitor_token()
    second = main.ensure_monitor_token()

    assert first == second
    assert len(first) > 20


def test_resolve_monitor_host_accepts_literal_host():
    assert main.resolve_monitor_host("127.0.0.1") == ("127.0.0.1", "127.0.0.1")


def test_resolve_monitor_host_uses_tailscale_ip(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args, 0, stdout="100.88.77.66\n", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    assert main.resolve_monitor_host("tailscale") == ("100.88.77.66", "100.88.77.66")


def test_resolve_monitor_host_falls_back_to_ifconfig(monkeypatch):
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args[0])
        if args[0] == "tailscale":
            raise FileNotFoundError
        return subprocess.CompletedProcess(
            args,
            0,
            stdout="utun4: flags=8051\n\tinet 100.101.102.103 netmask 0xffffffff\n",
            stderr="",
        )

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    assert main.resolve_monitor_host("tailscale") == ("100.101.102.103", "100.101.102.103")
    assert calls == ["tailscale", "ifconfig"]


def test_resolve_monitor_host_errors_when_tailscale_ip_missing(monkeypatch):
    def fake_run(args, **kwargs):
        if args[0] == "tailscale":
            raise FileNotFoundError
        return subprocess.CompletedProcess(args, 0, stdout="lo0: inet 127.0.0.1\n", stderr="")

    monkeypatch.setattr(main.subprocess, "run", fake_run)

    try:
        main.resolve_monitor_host("tailscale")
    except RuntimeError as exc:
        assert "Could not resolve a Tailscale IPv4 address" in str(exc)
    else:
        raise AssertionError("Expected RuntimeError")

import asyncio
import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest

import main
from tests.krx_english_sample import english_sample
from tests.test_long_term_research import universe_snapshot
from tradingagents.dataflows import naver_news
from tradingagents.research.long_term import write_json
from tradingagents.research.universe import import_csv, today_korea


@pytest.fixture
def setup(monkeypatch, tmp_path):
    monkeypatch.setattr(main, "LOCAL_DIR", tmp_path)
    monkeypatch.setattr(main, "is_regular_market_open", lambda *a, **kw: True)
    snapshot = import_csv({"csv": english_sample(), "asOf": today_korea().isoformat(),
                           "confirmed": True, "nonstandardCountConfirmed": True})
    path = tmp_path / "long_term" / "universe.json"
    write_json(path, snapshot)
    return path, snapshot


def article(title):
    return {"title": title, "description": "", "category": "selection_signals"}


def test_shared_registered_universe_replaces_static_list_and_preserves_source(setup, monkeypatch):
    path, source = setup
    original = path.read_bytes()
    news = Mock(return_value=[article("삼성전자 실적 개선"), article("0126Z0 수주"),
                             article("hanwha m&s 신규 수주"), article("에코프로 급등"), article("없는기업 999999")])
    monkeypatch.setattr(main, "collect_candidate_news_naver", news)
    args = main.make_parser().parse_args(["--run-mode", "once"])
    args.date = today_korea().isoformat()
    result = main.collect_candidate_snapshot(args)
    assert result["ok"] is True
    assert {row["ticker"] for row in result["candidates"]} == {"005930.KS", "0126Z0.KS", "0220W0.KS"}
    assert result["universe"] == source and path.read_bytes() == original
    assert "한글명/별칭" in result["warnings"][0]
    saved = json.loads(main.Path(result["jsonPath"]).read_text())
    assert saved["universe"] == source and len(saved["universe"]["members"]) == 201
    assert "KOSPI200 / 201" in main.Path(result["markdownPath"]).read_text()
    news.assert_called_once()


@pytest.mark.parametrize("kind", ["missing", "invalid-json", "future-date", "kospi100", "unconfirmed201"])
@pytest.mark.parametrize("execute", [False, True])
def test_invalid_source_never_fetches_news_or_falls_back_or_orders(setup, monkeypatch, kind, execute):
    path, snapshot = setup
    if kind == "missing":
        path.unlink()
    elif kind == "invalid-json":
        path.write_text("{broken")
    else:
        if kind == "future-date":
            snapshot["checkedOn"] = (today_korea() + timedelta(days=1)).isoformat()
        elif kind == "kospi100":
            snapshot["index"] = "KOSPI100"
        else:
            snapshot["nonstandardCountConfirmed"] = False
        write_json(path, snapshot)
    news = Mock(side_effect=AssertionError("must not query"))
    run = AsyncMock(side_effect=AssertionError("must not analyze or order"))
    monkeypatch.setattr(main, "collect_candidate_news_naver", news)
    monkeypatch.setattr(main, "run_korea_toss", run)
    args = main.make_parser().parse_args(["--run-mode", "once", "--yes"])
    result = asyncio.run(main.run_auto_trading(args, execute=execute))
    assert result["ok"] is False and result["selectionReason"]
    assert result["selectedTickers"] == result["analyses"] == []
    assert args.korea_ticker == "005930.KS"
    news.assert_not_called()
    run.assert_not_called()


@pytest.mark.parametrize("failure", [False, True])
@pytest.mark.parametrize("execute", [False, True])
def test_no_news_match_or_news_failure_never_uses_default_ticker(setup, monkeypatch, failure, execute):
    news = Mock(side_effect=RuntimeError("news unavailable")) if failure else Mock(return_value=[article("에코프로만 언급")])
    monkeypatch.setattr(main, "collect_candidate_news_naver", news)
    run = AsyncMock()
    monkeypatch.setattr(main, "run_korea_toss", run)
    args = main.make_parser().parse_args(["--run-mode", "once", "--yes"])
    result = asyncio.run(main.run_auto_trading(args, execute=execute))
    assert result["ok"] is (not failure)
    assert result["selectedTickers"] == result["analyses"] == []
    assert result["selectionReason"]
    run.assert_not_called()


@pytest.mark.parametrize("execute", [False, True])
def test_valid_candidates_reach_same_paper_and_live_pipeline(setup, monkeypatch, execute):
    monkeypatch.setattr(main, "screen_candidate_snapshot", lambda args, snapshot: {
        **snapshot, "candidates": [{**row, "intraday": {"eligible": True}} for row in snapshot["candidates"]]})
    path, snapshot = setup
    snapshot.update(asOf="2025-01-01", checkedOn="2025-01-02")
    write_json(path, snapshot)
    original = path.read_bytes()
    monkeypatch.setattr(main, "collect_candidate_news_naver", Mock(return_value=[article("0126Z0 실적"), article("삼성전자")]))
    run = AsyncMock(side_effect=lambda args, execute: {"ticker": args.korea_ticker, "ok": True})
    monkeypatch.setattr(main, "run_korea_toss", run)
    args = main.make_parser().parse_args(["--run-mode", "once", "--yes", "--trade-candidates", "1"])
    result = asyncio.run(main.run_auto_trading(args, execute=execute))
    assert result["ok"] and len(result["selectedTickers"]) == run.call_count == 1
    assert result["selectedTickers"][0] in setup[1]["members"]
    assert run.call_args.kwargs == {"execute": execute}
    assert path.read_bytes() == original


def test_outside_ticker_in_discovery_output_is_blocked_before_execution(setup, monkeypatch):
    monkeypatch.setattr(main, "collect_candidate_snapshot", lambda args: {
        "ok": True, "universe": setup[1], "candidates": [{"ticker": "999999.KS"}]})
    run = AsyncMock()
    monkeypatch.setattr(main, "run_korea_toss", run)
    args = main.make_parser().parse_args(["--run-mode", "once"])
    result = asyncio.run(main.run_auto_trading(args, execute=False))
    assert not result["ok"] and "구성목록 밖" in result["selectionReason"]
    run.assert_not_called()


def test_explicit_empty_map_does_not_restore_legacy_candidates():
    assert naver_news.rank_candidate_tickers([article("삼성전자")], symbol_map={}) == []


def test_market_suffix_and_alpha_codes_preserved_and_numeric_substrings_rejected():
    symbols = {"247540.KQ": "에코프로비엠", "0126Z0.KS": "SAMSUNG EPIS HOLDINGS", "005930.KS": "SamsungElec"}
    rows = naver_news.rank_candidate_tickers([article("에코프로비엠 0126Z0 90059300")], symbol_map=symbols)
    assert {row["ticker"] for row in rows} == {"247540.KQ", "0126Z0.KS"}


def test_aliases_cannot_introduce_outside_members():
    rows = naver_news.rank_candidate_tickers([article("삼성전자 SK하이닉스 네이버")], symbol_map={"005930.KS": "SamsungElec"})
    assert [row["ticker"] for row in rows] == ["005930.KS"]


def test_candidate_ui_shows_scope_failure_and_escaped_warning():
    html = main.render_monitor_html({"latestCandidates": {"universe": universe_snapshot(),
        "selectionReason": "코스피200 뉴스 후보 없음", "warnings": ["<script>unsafe</script>"]}})
    section = html.split('<section data-live="candidates">', 1)[1].split('</section>', 1)[0]
    assert "코스피200 · 200종목" in section and "뉴스 후보 없음" in section
    assert "&lt;script&gt;" in section and "<script>" not in section

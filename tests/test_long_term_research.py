from copy import deepcopy
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tradingagents.research import long_term as research
from tradingagents.research.service import ResearchBusy, ResearchService
from tradingagents.research.universe import today_korea


def universe_snapshot():
    members = {"005930.KS": "삼성전자", "000660.KS": "SK하이닉스"}
    members.update({f"{i:06}.KS": f"가상 종목 {i}" for i in range(1, 99)})
    return {"index": "KOSPI100", "asOf": today_korea().isoformat(), "checkedOn": today_korea().isoformat(),
            "source": "사용자 확인 KRX CSV", "members": members}


def market(**overrides):
    info = {"revenueGrowth": .15, "earningsGrowth": .12, "returnOnEquity": .13,
            "freeCashflow": 100000, "debtToEquity": 50, "trailingPE": 15,
            "priceToBook": 1.5, "currency": "KRW", "sector": "Technology",
            "mostRecentQuarter": datetime.now(timezone.utc).timestamp()}
    info.update(overrides)
    statements = {"income": {}, "cash": {}, "balance": {}}
    today = date.today()
    quarter_month = today.year * 12 + ((today.month - 1) // 3) * 3 - 1
    for index in range(8):
        year, month = divmod(quarter_month - index * 3, 12)
        day = date(year, month + 1, monthrange(year, month + 1)[1]).isoformat()
        statements["income"][day] = {"revenue": 180 - index * 10, "operatingIncome": (180 - index * 10) * (.2 - .005 * index), "netIncome": 10}
        statements["cash"][day] = {"operatingCashFlow": 20, "capex": -5, "freeCashFlow": 15}
        statements["balance"][day] = {"equity": 100, "currentAssets": 80, "currentLiabilities": 40, "shares": 100}
    return {"info": info, "closes": list(range(1, 301)), "priceDate": date.today().isoformat(),
            "statements": statements, "averageTurnover20": 1_000_000_000, "splitDates": []}


def config(**overrides):
    result = research.validate_request({"requestId": "a" * 32, "tickers": "005930.KS,000660.KS"}, {})
    result["universeSnapshot"] = universe_snapshot()
    result.update(overrides)
    return result


@pytest.mark.parametrize("horizon", research.HORIZONS)
def test_scoring_is_bounded_and_horizon_changes_weights(horizon):
    row = research.screen("005930.KS", market(), horizon)
    assert row["eligible"] is True
    assert row["score"] == row["coverage"] == 100
    assert sum(f["weight"] for f in row["facts"]) == 100
    assert next(f["weight"] for f in row["facts"] if f["id"] == "trend") == {"1-3": 25, "3-12": 15, "12-36": 5}[horizon]


@pytest.mark.parametrize("field", ["revenueGrowth", "earningsGrowth", "returnOnEquity", "freeCashflow", "debtToEquity", "trailingPE", "priceToBook"])
def test_missing_metrics_do_not_get_free_points(field):
    row = research.screen("005930.KS", market(**{field: None}), "3-12")
    assert row["coverage"] < 100 and row["missing"]
    assert not any(f["id"] == field for f in row["facts"])


@pytest.mark.parametrize("info", [{"mostRecentQuarter": None}, {"mostRecentQuarter": 1},
    {"sector": "Financial Services"}, {"sector": "Real Estate"}, {"currency": "USD"}])
def test_unreliable_or_noncomparable_data_is_not_a_candidate(info):
    row = research.screen("005930.KS", market(**info), "3-12")
    assert row["eligible"] is False and row["warnings"]


def test_stale_prices_and_missing_financials_are_explicit():
    data = market()
    data["priceDate"] = (date.today() - timedelta(days=30)).isoformat()
    assert not research.screen("005930.KS", data, "3-12")["eligible"]
    data["closes"] = [1, 2]
    data["info"] = {}
    row = research.screen("005930.KS", data, "3-12")
    assert row["score"] == row["coverage"] == 0 and len(row["missing"]) == 8


def test_negative_values_do_not_count_as_cheap_or_safe():
    row = research.screen("005930.KS", market(trailingPE=-3, priceToBook=-2, debtToEquity=-20, freeCashflow=-1), "3-12")
    assert all(f["points"] == 0 for f in row["facts"] if f["id"] in {"trailingPE", "priceToBook", "debtToEquity", "freeCashflow"})


@pytest.mark.parametrize("change", [{"requestId": "../bad"}, {"tickers": "005930"}, {"tickers": "AAPL"},
    {"tickers": "005930.KS;rm"}, {"horizon": "daily"}, {"horizon": []}, {"provider": "hybrid"},
    {"tickers": ["005930.KS"]}, {"tickers": "247540.KQ"}, {"tickers": ",".join(f"{n:06}.KS" for n in range(101))}])
def test_invalid_requests_rejected(change):
    with pytest.raises(ValueError):
        research.validate_request({"requestId": "a" * 32, **change}, {})


def test_configuration_is_independent_and_restricted_to_kospi():
    settings = {"openai_quick_model": "test-model", "paper_capital": "10000000", "api_key": "secret"}
    before = deepcopy(settings)
    job = research.validate_request({"requestId": "a" * 32, "provider": "openai", "tickers": "005930.ks 005930.KS"}, settings)
    assert job["tickers"] == ["005930.KS"] and job["model"] == "test-model"
    assert "secret" not in json.dumps(job) and "paper_capital" not in job
    assert settings == before
    automatic = research.validate_request({"requestId": "b" * 32}, {})
    assert automatic["tickers"] == [] and automatic["universe"] == "KOSPI100"


def test_optional_llm_and_partial_failures_preserve_quantitative_results():
    def fetch(ticker):
        if ticker == "000660.KS":
            raise OSError("credentials must not leak")
        return market()
    reviewer = Mock(side_effect=TimeoutError("api key"))
    result = research.run_research(config(provider="openai"), fetch=fetch, review=reviewer)
    reviewer.assert_called_once()
    assert result["shortlist"] == ["005930.KS"]
    assert len(result["errors"]) == 1 and "api key" not in json.dumps(result)
    assert "실패" in result["llmStatus"] and result["readOnly"]
    reviewer.reset_mock()
    research.run_research(config(), fetch=fetch, review=reviewer)
    reviewer.assert_not_called()


@pytest.mark.parametrize("item", [
    {"ticker": "005930.KS", "supporting_ids": ["invented"], "risk_ids": [], "question_ids": []},
    {"ticker": "005930.KS", "supporting_ids": [], "risk_ids": ["trend"], "question_ids": []},
    {"ticker": "005930.KS", "supporting_ids": [], "risk_ids": [], "question_ids": ["buy now"]},
    {"ticker": "005930.KS", "supporting_ids": [], "risk_ids": [], "question_ids": [], "claim": "guaranteed gain"},
    {"ticker": "OTHER", "supporting_ids": [], "risk_ids": [], "question_ids": []},
])
def test_llm_cannot_publish_unverified_claims(monkeypatch, item):
    import tradingagents.llm_clients
    llm = Mock()
    llm.invoke.return_value = SimpleNamespace(content=json.dumps({"reviews": [item]}))
    monkeypatch.setattr(tradingagents.llm_clients, "create_llm_client", lambda *a, **kw: SimpleNamespace(get_llm=lambda: llm))
    rows = [research.screen("005930.KS", market(), "3-12")]
    with pytest.raises(ValueError):
        research.review_candidates(rows, config(provider="openai"))
    assert "review" not in rows[0]


def test_llm_can_only_select_existing_evidence(monkeypatch):
    import tradingagents.llm_clients
    item = {"ticker": "005930.KS", "supporting_ids": ["trend", "freeCashflow"], "risk_ids": [], "question_ids": ["earnings"]}
    llm = Mock()
    llm.invoke.return_value = SimpleNamespace(content=json.dumps({"reviews": [item]}))
    monkeypatch.setattr(tradingagents.llm_clients, "create_llm_client", lambda *a, **kw: SimpleNamespace(get_llm=lambda: llm))
    rows = [research.screen("005930.KS", market(), "3-12")]
    research.review_candidates(rows, config(provider="openai"))
    assert rows[0]["review"] == item


def test_service_never_mutates_trading_files_and_marks_interrupted(tmp_path):
    sentinel = tmp_path / "scheduler.json"
    sentinel.write_text('{"untouched":true}')
    service = ResearchService(tmp_path / "long_term", tmp_path)
    job = "a" * 32
    research.write_json(service.root / "latest.json", {"id": job})
    research.write_json(service.root / job / "status.json", {"id": job, "status": "running"})
    state = service.snapshot()
    assert state["status"]["status"] == "interrupted" and not state["active"]
    assert sentinel.read_text() == '{"untouched":true}'
    with pytest.raises(ValueError):
        service.snapshot("../../config")


def test_cross_process_lock_prevents_double_research(tmp_path):
    first = ResearchService(tmp_path, tmp_path)
    second = ResearchService(tmp_path, tmp_path)
    lock = first._file_lock()
    try:
        with pytest.raises(ResearchBusy):
            second.submit({"requestId": "a" * 32}, {})
    finally:
        lock.close()


def test_service_launch_stop_idempotence_and_no_shell(tmp_path, monkeypatch):
    import tradingagents.research.service as module
    process = Mock()
    process.poll.return_value = None
    popen = Mock(return_value=process)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module.threading, "Thread", Mock())
    service = ResearchService(tmp_path / "long_term", tmp_path)
    payload = {"requestId": "a" * 32}
    service.submit(payload, {})
    service.submit(payload, {})
    popen.assert_called_once()
    args, kwargs = popen.call_args
    assert args[0][1:3] == ["-m", "tradingagents.research.long_term"]
    assert not kwargs.get("shell") and kwargs["pass_fds"]
    with pytest.raises(ValueError):
        service.submit({**payload, "horizon": "12-36"}, {})
    state = service.stop(payload["requestId"])
    assert state["status"]["status"] == "stopped"
    process.terminate.assert_called_once()


def test_worker_saves_only_research_artifacts(tmp_path, monkeypatch):
    import yfinance
    monkeypatch.setattr(research.os, "nice", lambda _: None)
    monkeypatch.setattr(research.signal, "alarm", lambda _: None)
    monkeypatch.setattr(yfinance, "set_tz_cache_location", lambda _: None)
    monkeypatch.setattr(research, "resolve_universe", lambda _: universe_snapshot())
    job = tmp_path / ("a" * 32)
    research.write_json(job / "request.json", config())
    monkeypatch.setattr(research, "run_research", lambda *a, **kw: {"rows": [{"ticker": "005930.KS"}]})
    research.worker(job)
    assert json.loads((job / "status.json").read_text())["status"] == "completed"
    assert {p.name for p in job.iterdir()} == {"request.json", "report.json", "status.json", "universe.json"}


def test_real_subprocess_retains_lock_until_cancelled(tmp_path, monkeypatch):
    import tradingagents.research.service as module
    original = module.subprocess.Popen
    def offline_process(args, **kwargs):
        return original([args[0], "-c", "import time; time.sleep(60)"], **kwargs)
    monkeypatch.setattr(module.subprocess, "Popen", offline_process)
    service = ResearchService(tmp_path / "long_term", tmp_path)
    other = ResearchService(service.root, tmp_path)
    try:
        state = service.submit({"requestId": "a" * 32}, {})
        assert state["active"]["status"] == "running"
        with pytest.raises(ResearchBusy):
            other.submit({"requestId": "b" * 32}, {})
    finally:
        service.close()
    assert service.process.poll() is not None
    assert service.snapshot()["status"]["status"] == "stopped"

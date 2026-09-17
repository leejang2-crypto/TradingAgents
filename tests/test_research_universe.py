import json
from datetime import date, timedelta
from unittest.mock import Mock

import pytest
import requests

from tests.test_long_term_research import config, market, universe_snapshot
from tradingagents.research import long_term, universe
from tradingagents.research.service import ResearchBusy, ResearchService


def csv_payload():
    snapshot = universe_snapshot()
    return {"csv": "종목코드,종목명\n" + "\n".join(f"{code[:6]},{name}" for code, name in snapshot["members"].items()),
            "asOf": snapshot["asOf"], "confirmed": True}


def test_csv_preserves_leading_zero_and_explicit_source():
    payload = csv_payload()
    payload["csv"] = "\ufeff" + payload["csv"]
    result = universe.import_csv(payload)
    assert result["members"] == universe_snapshot()["members"]
    assert result["source"] == "사용자 확인 KRX CSV"
    assert result["checkedOn"] == universe.today_korea().isoformat()


@pytest.mark.parametrize("change", [
    {"confirmed": False}, {"confirmed": "true"}, {"csv": None}, {"csv": "a" * 120001},
    {"csv": "종목코드,종목명\n5930,삼성전자"}, {"csv": "ticker,name\n005930,test"},
    {"csv": '종목코드,종목명\n005930,"unclosed'}, {"asOf": "invalid"},
    {"asOf": (universe.today_korea() + timedelta(days=1)).isoformat()},
    {"asOf": (universe.today_korea() - timedelta(days=11)).isoformat()},
])
def test_csv_rejects_invalid_input(change):
    with pytest.raises(universe.UniverseError):
        universe.import_csv({**csv_payload(), **change})


@pytest.mark.parametrize("kind", ["duplicate", "missing", "extra", "empty-name"])
def test_csv_requires_exactly_100_distinct_named_members(kind):
    payload = csv_payload()
    lines = payload["csv"].splitlines()
    if kind == "duplicate":
        lines[-1] = lines[1]
    elif kind == "missing":
        lines.pop()
    elif kind == "extra":
        lines.append("999999,extra")
    else:
        lines[-1] = "000098,"
    payload["csv"] = "\n".join(lines)
    with pytest.raises(universe.UniverseError):
        universe.import_csv(payload)


@pytest.mark.parametrize("change", [{"index": "KRX100"}, {"source": []}, {"checkedOn": "2000-01-01"}, {"members": {}}, {"asOf": None}])
def test_snapshot_rejects_wrong_index_or_stale_or_malformed_data(change):
    with pytest.raises(universe.UniverseError):
        universe.validate_snapshot({**universe_snapshot(), **change})


def krx_rows():
    return [{"ISU_SRT_CD": code[:6], "ISU_ABBRV": name} for code, name in universe_snapshot()["members"].items()]


def test_official_fetch_uses_kospi100_and_skips_weekends_and_empty_holidays(monkeypatch):
    post = Mock(side_effect=[Mock(json=lambda: {"output": []}), Mock(json=lambda: {"output": krx_rows()})])
    monkeypatch.setattr(universe.requests, "post", post)
    result = universe.fetch_krx(today=date(2026, 9, 21))
    assert result["asOf"] == "2026-09-18" and result["checkedOn"] == "2026-09-21"
    assert result["source"] == "KRX 자동 조회"
    first, second = [call.kwargs for call in post.call_args_list]
    assert first["data"]["indIdx"] == "1" and first["data"]["indIdx2"] == "034"
    assert first["data"]["trdDd"] == "20260921" and second["data"]["trdDd"] == "20260918"
    assert first["timeout"] == 15


def test_krx_http_failure_does_not_retry_or_use_stale_snapshot(monkeypatch):
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("403")
    post = Mock(return_value=response)
    monkeypatch.setattr(universe.requests, "post", post)
    cached = {**universe_snapshot(), "checkedOn": "2000-01-01"}
    with pytest.raises(universe.UniverseError, match="CSV"):
        universe.resolve_universe(cached, today=date(2026, 9, 17))
    assert post.call_count == 1


@pytest.mark.parametrize("response", [{"output": krx_rows()[:-1]}, {"output": krx_rows() + [krx_rows()[0]]}, {"output": None}, []])
def test_krx_invalid_response_is_not_used(monkeypatch, response):
    monkeypatch.setattr(universe.requests, "post", Mock(return_value=Mock(json=lambda: response)))
    with pytest.raises(universe.UniverseError):
        universe.fetch_krx(today=date(2026, 9, 17))


def test_valid_same_day_cache_needs_no_network():
    fetch = Mock(side_effect=AssertionError("unexpected network"))
    assert universe.resolve_universe(universe_snapshot(), fetch=fetch) == universe_snapshot()
    fetch.assert_not_called()


def test_all_100_default_and_subset_with_official_names():
    fetch = Mock(side_effect=lambda ticker: market(shortName="not the official name"))
    report = long_term.run_research(config(tickers=[]), fetch=fetch)
    assert len(report["scope"]) == len(report["rows"]) == fetch.call_count == 100
    assert set(report["scope"]) == set(universe_snapshot()["members"])
    assert report["universe"] == universe_snapshot()
    assert next(row["name"] for row in report["rows"] if row["ticker"] == "005930.KS") == "삼성전자"
    assert universe.select_members(["005930.KS"], universe_snapshot()) == ["005930.KS"]


@pytest.mark.parametrize("snapshot,tickers", [(None, []), (universe_snapshot(), ["999999.KS"])])
def test_unverified_universe_or_outsiders_fail_before_market_or_llm(snapshot, tickers):
    fetch, review = Mock(), Mock()
    with pytest.raises(universe.UniverseError):
        long_term.run_research(config(universeSnapshot=snapshot, tickers=tickers), fetch=fetch, review=review)
    fetch.assert_not_called()
    review.assert_not_called()


def test_upload_uses_research_lock_and_preserves_existing_snapshot(tmp_path):
    service = ResearchService(tmp_path, tmp_path)
    result = service.register_universe(csv_payload())
    assert result["universe"]["members"] == universe_snapshot()["members"]
    before = (tmp_path / "universe.json").read_bytes()
    stream = service._file_lock()
    try:
        with pytest.raises(ResearchBusy):
            service.register_universe(csv_payload())
    finally:
        stream.close()
    assert (tmp_path / "universe.json").read_bytes() == before


def test_worker_reports_source_failure_without_market_fetch(tmp_path, monkeypatch):
    job = tmp_path / ("c" * 32)
    long_term.write_json(job / "request.json", config())
    monkeypatch.setattr(long_term.os, "nice", lambda _: None)
    monkeypatch.setattr(long_term.signal, "alarm", lambda _: None)
    monkeypatch.setattr(long_term, "resolve_universe", Mock(side_effect=universe.UniverseError("KRX CSV 필요")))
    run = Mock()
    monkeypatch.setattr(long_term, "run_research", run)
    long_term.worker(job)
    status = json.loads((job / "status.json").read_text())
    assert status["status"] == "failed" and "KRX CSV" in status["phase"]
    run.assert_not_called()
    assert not (job / "report.json").exists()

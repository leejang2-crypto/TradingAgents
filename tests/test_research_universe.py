import json
from datetime import date, datetime, timedelta
from unittest.mock import Mock

import pytest
import requests

from tests.krx_english_sample import ISSUE_CODES, english_sample
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
    {"fileName": 123}, {"fileName": "../"}, {"fileName": "bad\n.csv"}, {"fileName": "a" * 256},
])
def test_csv_rejects_invalid_input(change):
    with pytest.raises(universe.UniverseError):
        universe.import_csv({**csv_payload(), **change})


@pytest.mark.parametrize("kind", ["duplicate", "missing", "extra", "empty-name"])
def test_csv_requires_exactly_200_distinct_named_members(kind):
    payload = csv_payload()
    lines = payload["csv"].splitlines()
    if kind == "duplicate":
        lines[-1] = lines[1]
    elif kind == "missing":
        lines.pop()
    elif kind == "extra":
        lines.append("999999,extra")
    else:
        lines[-1] = "000198,"
    payload["csv"] = "\n".join(lines)
    with pytest.raises(universe.UniverseError):
        universe.import_csv(payload)


@pytest.mark.parametrize("change", [{"index": "KOSPI100"}, {"index": "KRX100"}, {"source": []}, {"checkedOn": "2000-01-01"}, {"members": {}}, {"asOf": None}])
def test_snapshot_rejects_wrong_index_or_stale_or_malformed_data(change):
    with pytest.raises(universe.UniverseError):
        universe.validate_snapshot({**universe_snapshot(), **change})


def krx_rows():
    return [{"ISU_SRT_CD": code[:6], "ISU_ABBRV": name} for code, name in universe_snapshot()["members"].items()]


def test_official_fetch_uses_kospi200_and_skips_weekends_and_empty_holidays(monkeypatch):
    post = Mock(side_effect=[Mock(json=lambda: {"output": []}), Mock(json=lambda: {"output": krx_rows()})])
    monkeypatch.setattr(universe.requests, "post", post)
    result = universe.fetch_krx(today=date(2026, 9, 21))
    assert result["asOf"] == "2026-09-18" and result["checkedOn"] == "2026-09-21"
    assert result["source"] == "KRX 자동 조회"
    first, second = [call.kwargs for call in post.call_args_list]
    assert first["data"]["indIdx"] == "1" and first["data"]["indIdx2"] == "028"
    assert first["data"]["trdDd"] == "20260921" and second["data"]["trdDd"] == "20260918"
    assert first["timeout"] == 15


def test_krx_http_failure_does_not_retry_or_use_stale_snapshot(monkeypatch):
    response = Mock()
    response.raise_for_status.side_effect = requests.HTTPError("403")
    post = Mock(return_value=response)
    monkeypatch.setattr(universe.requests, "post", post)
    cached = {**universe_snapshot(), "source": "KRX 자동 조회", "checkedOn": "2000-01-01"}
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


def test_old_uploaded_kospi100_cache_requires_manual_replacement():
    old = {**universe_snapshot(), "index": "KOSPI100"}
    old["members"] = dict(list(old["members"].items())[:100])
    current = universe_snapshot()
    fetch = Mock(return_value=current)
    with pytest.raises(universe.UniverseError):
        universe.resolve_universe(old, fetch=fetch)
    fetch.assert_not_called()
    assert old["index"] == "KOSPI100" and len(old["members"]) == 100
    fetch.side_effect = universe.UniverseError("KRX CSV 필요")
    with pytest.raises(universe.UniverseError):
        universe.resolve_universe(old, fetch=fetch)


def test_uploaded_csv_persists_across_dates_without_automatic_refresh():
    snapshot = universe.import_csv({**csv_payload(), "asOf": "2025-01-01", "fileName": "kospi200.csv"}, today=date(2025, 1, 2))
    fetch = Mock(side_effect=AssertionError("must not replace uploaded CSV"))
    assert universe.resolve_universe(snapshot, fetch=fetch, today=date(2026, 9, 22)) == snapshot
    fetch.assert_not_called()
    broken = {**snapshot, "members": {}}
    with pytest.raises(universe.UniverseError):
        universe.resolve_universe(broken, fetch=fetch)
    fetch.assert_not_called()


def test_upload_metadata_survives_restart_and_only_successful_registration_replaces_it(tmp_path):
    service = ResearchService(tmp_path, tmp_path)
    before = datetime.now().astimezone()
    first = service.register_universe({**csv_payload(), "asOf": "2025-01-01", "fileName": r"C:\fakepath\kospi200.csv",
                                       "registeredAt": "2000-01-01T00:00:00Z"})["universe"]
    after = datetime.now().astimezone()
    assert first["fileName"] == "kospi200.csv"
    registered = datetime.fromisoformat(first["registeredAt"])
    assert before.replace(microsecond=0) <= registered <= after
    assert registered.utcoffset() == timedelta(hours=9)
    restarted = ResearchService(tmp_path, tmp_path)
    assert restarted.snapshot()["universe"] == first
    with pytest.raises(universe.UniverseError):
        restarted.register_universe({**csv_payload(), "fileName": "invalid.csv", "csv": "invalid"})
    assert restarted.snapshot()["universe"] == first
    second = restarted.register_universe({**csv_payload(), "fileName": "/tmp/new.csv"})["universe"]
    assert second["fileName"] == "new.csv" and second["asOf"] == csv_payload()["asOf"]


def test_legacy_uploaded_csv_without_metadata_remains_usable():
    snapshot = {**universe_snapshot(), "asOf": "2025-01-01", "checkedOn": "2025-01-02"}
    assert universe.resolve_universe(snapshot) == snapshot
    assert "fileName" not in snapshot and "registeredAt" not in snapshot


def test_kospi100_csv_is_rejected_and_200_explicit_tickers_are_supported():
    payload = csv_payload()
    payload["csv"] = "\n".join(payload["csv"].splitlines()[:101])
    with pytest.raises(universe.UniverseError, match="200"):
        universe.import_csv(payload)
    tickers = list(universe_snapshot()["members"])
    request = long_term.validate_request({"requestId": "d" * 32, "tickers": ", ".join(tickers)}, {})
    assert request["tickers"] == tickers and request["universe"] == "KOSPI200"


def test_all_200_default_and_subset_with_official_names():
    fetch = Mock(side_effect=lambda ticker: market(shortName="not the official name"))
    report = long_term.run_research(config(tickers=[]), fetch=fetch)
    assert len(report["scope"]) == len(report["rows"]) == fetch.call_count == 200
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


@pytest.mark.parametrize("delimiter", [",", "\t"])
def test_user_english_codes_are_all_preserved_and_201_requires_confirmation(delimiter):
    payload = {**csv_payload(), "csv": english_sample(delimiter)}
    with pytest.raises(universe.UniverseError, match="201종목 원본 확인"):
        universe.import_csv(payload)
    snapshot = universe.import_csv({**payload, "nonstandardCountConfirmed": True})
    assert len(ISSUE_CODES) == len(set(ISSUE_CODES)) == len(snapshot["members"]) == 201
    assert set(snapshot["members"]) == {code.zfill(6) + ".KS" for code in ISSUE_CODES}
    assert snapshot["members"]["005930.KS"] == "SamsungElec"
    assert snapshot["members"]["000660.KS"] == "SK hynix"
    assert snapshot["members"]["006400.KS"] == "SAMSUNG SDI CO.,LTD."
    assert snapshot["members"]["0126Z0.KS"] == "SAMSUNG EPIS HOLDINGS"
    assert snapshot["members"]["0220W0.KS"] == "Hanwha M&S"
    assert snapshot["members"]["483650.KS"] == "d'Alba Global"
    assert snapshot["importInfo"] == {"rowCount": 201, "zeroPaddedCodes": sum(len(c) < 6 for c in ISSUE_CODES),
                                     "alphanumericCodes": 2, "format": "TSV" if delimiter == "\t" else "CSV"}


def test_201_rows_flow_through_full_research_and_optional_alpha_subset(tmp_path):
    service = ResearchService(tmp_path, tmp_path)
    state = service.register_universe({**csv_payload(), "csv": english_sample(), "nonstandardCountConfirmed": True})
    snapshot = state["universe"]
    request = long_term.validate_request({"requestId": "e" * 32, "tickers": ", ".join(snapshot["members"])}, {})
    assert len(request["tickers"]) == 201
    fetch, review = Mock(side_effect=lambda _: market()), Mock()
    report = long_term.run_research(config(tickers=[], universeSnapshot=snapshot, provider="openai"), fetch=fetch, review=review)
    assert len(report["rows"]) == fetch.call_count == len(report["scope"]) == 201
    assert "0126Z0.KS" in report["scope"] and "0220W0.KS" in report["scope"]
    review.assert_called_once()
    assert len(review.call_args.args[0]) == 5
    subset = long_term.validate_request({"requestId": "f" * 32, "tickers": "0126z0.ks, 0220W0.KS"}, {})
    assert universe.select_members(subset["tickers"], snapshot) == ["0126Z0.KS", "0220W0.KS"]


def test_official_201_constituents_accept_alphanumeric_codes(monkeypatch):
    rows = krx_rows() + [{"ISU_SRT_CD": "0126Z0", "ISU_ABBRV": "SAMSUNG EPIS HOLDINGS"}]
    monkeypatch.setattr(universe.requests, "post", Mock(return_value=Mock(json=lambda: {"output": rows})))
    result = universe.fetch_krx(today=date(2026, 9, 18))
    assert len(result["members"]) == 201 and result["source"] == "KRX 자동 조회"


@pytest.mark.parametrize("code", ["5930.0", "5.93E3", "=5930", '="005930"', "-5930", "1234567", "１２３４５６", "../foo", "0126Z"])
def test_ambiguous_or_unsafe_codes_are_never_guessed(code):
    payload = {**csv_payload(), "csv": csv_payload()["csv"].replace("005930,", code + ",", 1)}
    with pytest.raises(universe.UniverseError, match="2행"):
        universe.import_csv(payload)


def test_duplicate_after_zero_padding_is_rejected():
    payload = csv_payload()
    payload["csv"] = payload["csv"].replace("000660,", "5930,", 1)
    with pytest.raises(universe.UniverseError, match="005930.*중복"):
        universe.import_csv(payload)


def test_count_exception_does_not_accept_arbitrary_or_incomplete_lists():
    payload = {**csv_payload(), "nonstandardCountConfirmed": True}
    for text in ("\n".join(payload["csv"].splitlines()[:200]), english_sample() + "999999\textra\n"):
        with pytest.raises(universe.UniverseError):
            universe.import_csv({**payload, "csv": text})


def test_english_headers_case_space_bom_and_quoted_tabs():
    payload = {**csv_payload(), "nonstandardCountConfirmed": True}
    text = english_sample().replace("Issue code\tIssue name", '" ISSUE CODE "\t" issue name "', 1)
    snapshot = universe.import_csv({**payload, "csv": "\ufeff" + text + "\t\t\n"})
    assert len(snapshot["members"]) == 201

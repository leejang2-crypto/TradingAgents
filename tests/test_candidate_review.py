from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

import main
from tests.test_intraday import data
from tests.test_intraday_analysis import technical, config, material
from tests.test_postmarket import archive, DAY, NOW
from tradingagents.research import candidate_review as paths, postmarket
from tradingagents.brokers import intraday_analysis as fast


@pytest.fixture
def study(data):
    rows, _, _, start, now = deepcopy(data)
    cfg = {**paths.INTRADAY_DEFAULTS, "min_session_turnover": "1000000", "market_close": "09:40",
           "buy_cutoff_minutes": 0, "liquidation_minutes": 0}
    for minute in range(21, 41):
        rows.append({"timestamp": (start + timedelta(minutes=minute)).isoformat(), "currency": "KRW",
                     "openPrice": "10060", "highPrice": "10200" if minute == 24 else "10070",
                     "lowPrice": "10050", "closePrice": "10170" if minute == 24 else "10060", "volume": "1000"})
    replay = {"start": start.isoformat(), "asOf": now.isoformat(), "settings": cfg, "candles": deepcopy(rows[:20])}
    inputs = {"riskState": {"orders": {}}, "summaries": [], "replays": [], "candidates": [{
        "intradayScreen": {"asOf": now.isoformat(), "settings": cfg}, "candidates": [{
            "ticker": "005930.KS", "name": "삼성전자", "intraday": {"replay": replay, "reasons": ["then-HOLD"]}}]}],
        "market": {"status": "complete", "calendar": {"today": {"date": str(start.date()), "integrated": {
            "regularMarket": {"startTime": start.isoformat(), "endTime": (start + timedelta(minutes=40)).isoformat()}}}},
            "indices": {key: {"candles": deepcopy(rows)} for key in ("KOSPI", "KOSDAQ")}},
        "candidatePrices": {"005930": {"status": "complete", "candles": rows}}}
    for index in inputs["market"]["indices"].values():
        index["reference"] = paths.market_reference(index["candles"], start, start + timedelta(minutes=40))
    return inputs, start, now, cfg


def result(study):
    return paths.analyze(study[0], study[1].date())["candidates"][0]


def configured(row):
    return next(window for window in row["windows"] if window["configuredStrategy"])


def test_target_window_entry_is_after_known_candidate_and_signal(study):
    row = result(study)
    assert row["status"] == "complete" and row["targetWindows"] >= 1
    window = configured(row)
    assert window["entryAt"][11:19] == "09:21:00"
    assert window["barEnd"][11:19] == "09:20:00"
    assert window["exitAt"][11:19] == "09:24:00"
    assert window["outcome"] == "target" and Decimal(window["estimatedNetReturn"]) == Decimal(".007")
    assert window["marketCheck"] == "passed" and window["observedReasons"] == ["then-HOLD"]
    assert window["ruleKey"] == paths.rule_key(study[3])
    assert not row["executionVerified"] and row["scope"] == paths.SCOPE


def test_new_entry_start_excludes_earlier_hindsight_profit(study):
    study[3]['buy_start_minutes'] = 60
    study[3]['candidate_scan_minutes'] = 60
    row = result(study)
    assert not row['windows'] and not row['priceRange']['firstTarget']


def test_time_policy_changes_do_not_reuse_old_learning(study):
    old = postmarket.strategy_context(study[3])
    old_key = paths.rule_key(study[3])
    study[3].update(buy_start_minutes=60, candidate_scan_minutes=60, buy_cutoff_minutes=60)
    assert postmarket.strategy_context(study[3])['fingerprint'] != old['fingerprint']
    assert paths.rule_key(study[3]) != old_key


@pytest.mark.parametrize("case,expected", [("stop-first", "stop"), ("both", "ambiguous"), ("gap-stop", "stop")])
def test_barrier_order_is_conservative(study, case, expected):
    rows = study[0]["candidatePrices"]["005930"]["candles"]
    if case == "stop-first":
        rows[22].update(lowPrice="10000")
    elif case == "both":
        rows[23].update(lowPrice="10000")
    else:
        rows[22].update(openPrice="9900", lowPrice="9890")
    window = configured(result(study))
    assert window["outcome"] == expected
    if case == "both":
        assert window["estimatedNetReturn"] is None
    if case == "gap-stop":
        assert window["exitPrice"] == "9900"


def test_no_future_bar_changes_past_signal(study):
    before = configured(result(study))
    study[0]["candidatePrices"]["005930"]["candles"][30].update(highPrice="999999")
    assert configured(result(study)) == before


def test_late_candidate_does_not_capture_earlier_profit(study):
    snapshot = study[0]["candidates"][0]
    at = study[1] + timedelta(minutes=26)
    snapshot["intradayScreen"]["asOf"] = at.isoformat()
    snapshot["candidates"][0]["intraday"]["replay"].update(asOf=at.isoformat())
    row = result(study)
    assert not row.get("targetWindows") and not row["priceRange"]["firstTarget"]
    assert all(paths.stamp(window["entryAt"]) >= at for window in row["windows"])


def test_hindsight_price_rise_is_separate_from_rule_opportunity(study):
    study[3]["min_relative_volume"] = "100"
    row = result(study)
    assert row["priceRange"]["firstTarget"] and not row["windows"]
    assert row["blockReasons"]
    assert "가격상 목표 구간은 있었지만" in paths.markdown(paths.analyze(study[0], study[1].date()))


def test_costs_can_reject_a_hindsight_winner(study):
    study[3]["estimated_cost_rate"] = ".02"
    row = result(study)
    assert not row["windows"] and row["priceRange"]["firstTarget"]
    assert any("손익비" in reason for reason, _ in row["blockReasons"])


def test_missing_data_never_means_no_opportunity(study):
    study[0]["candidatePrices"]["005930"]["candles"].pop(25)
    row = result(study)
    assert row["status"] == "unavailable" and "분봉" in row["reason"]
    assert not row["windows"]


def test_offline_prefix_is_partial_not_full_day(study):
    study[0]["candidatePrices"] = {}
    row = result(study)
    assert row["status"] == "partial" and row["source"] == "saved-prefix"
    assert row["evaluatedThrough"][11:16] == "09:20"
    assert not row["windows"]


def test_old_data_with_no_observed_settings_cannot_invent_signal(study):
    snapshot = study[0]["candidates"][0]
    snapshot["intradayScreen"].pop("settings")
    snapshot["candidates"][0]["intraday"].pop("replay")
    assert result(study)["status"] == "unavailable"


def test_later_settings_not_used_early(study):
    snapshot = study[0]["candidates"][0]
    later = deepcopy(snapshot)
    later["intradayScreen"]["asOf"] = (study[1] + timedelta(minutes=30)).isoformat()
    later["candidates"][0]["intraday"]["replay"].update(asOf=later["intradayScreen"]["asOf"])
    later["candidates"][0]["intraday"]["replay"]["settings"]["min_relative_volume"] = "100"
    study[0]["candidates"].append(later)
    assert configured(result(study))["entryAt"][11:16] == "09:21"


def test_recorded_hold_is_distinct_and_future_decision_not_attributed(study):
    inputs, start, _, _ = study
    inputs["summaries"] = [{"analyses": [
        {"ticker": "005930.KS", "intradayContract": {"evaluatedAt": (start + timedelta(minutes=20)).isoformat(),
          "action": "HOLD", "reasons": ["뉴스 위험"]}},
        {"ticker": "005930.KS", "intradayContract": {"evaluatedAt": (start + timedelta(minutes=30)).isoformat(),
          "action": "BUY", "reasons": ["later"]}}]}]
    window = configured(result(study))
    assert window["recordedDecision"]["action"] == "HOLD"
    assert window["outcome"] == "target"


def test_price_collection_reads_only_actual_candidates_and_stops_on_429(study, monkeypatch):
    inputs, start, _, _ = study
    second = deepcopy(inputs["candidates"][0]["candidates"][0])
    second["ticker"] = "035420.KS"
    inputs["candidates"][0]["candidates"].append(second)
    backend = Mock()
    backend.clock.return_value = start + timedelta(hours=7)
    class Limited(Exception):
        status_code = 429
    fetch = Mock(side_effect=Limited())
    monkeypatch.setattr(paths, "session_candles", fetch)
    collected = paths.collect_prices(inputs, start.date(), backend)
    assert fetch.call_count == 1
    assert collected["035420"]["status"] == "deferred"
    assert collected["035420"]["reason"] == "rate-limit"
    backend.call.assert_not_called()


def test_no_backend_is_offline_and_no_order_method_is_called(study, monkeypatch):
    fetch = Mock(side_effect=AssertionError("no API calls"))
    monkeypatch.setattr(paths, "session_candles", fetch)
    assert paths.collect_prices(study[0], study[1].date())["005930"]["status"] == "unavailable"
    fetch.assert_not_called()


def test_hypothetical_targets_never_teach_actual_sizing(study):
    result = postmarket.analyze_day(study[0], study[1].date(), "dry-run", "paper")
    assert result["candidateReview"]["targetCandidateCount"] == 1
    assert result["roundTrips"] == 0 and result["netRealizedPnl"] == "0"
    assert not result["policy"]["evidence"] and not result["policy"]["adjustments"]


@pytest.mark.parametrize("case", ["valid", "same-day", "expired", "other-mode", "other-account", "different-rule", "partial", "corrupt"])
def test_next_day_reference_scoping(tmp_path, study, case):
    recap = paths.compact(paths.analyze(study[0], study[1].date()))
    record = {"mode": "dry-run", "account": "paper", "candidateReview": recap}
    now, cfg, mode, account = study[2] + timedelta(days=1), deepcopy(study[3]), "dry-run", "paper"
    if case == "same-day": now = study[2]
    if case == "expired": now += timedelta(days=8)
    if case == "other-mode": mode = "live"
    if case == "other-account": account = "other"
    if case == "different-rule": cfg["position_target_return"] = ".02"
    if case == "partial": recap["candidates"][0]["status"] = "partial"
    if case == "corrupt": record["candidateReview"] = None
    postmarket.atomic_json(postmarket.state_path(tmp_path, "dry-run", "paper"), record)
    reference = paths.prior_reference(tmp_path, mode, account, "005930.KS", now, cfg)
    if case == "valid":
        assert reference["referenceOnly"] and reference["matchingWindows"]
    elif case == "different-rule":
        assert not reference["matchingWindows"]
    else:
        assert reference is None


def test_reference_is_in_saved_fast_report_not_action_or_news_prompt(technical, config, material, tmp_path):
    review = {"ticker": technical["ticker"], "date": technical["date"], "items": [{"id": 1, "signal": "neutral"}]}
    baseline = fast.build_state(technical["ticker"], technical["date"], config, technical, review, material)
    config["candidate_path_reference"] = {"referenceOnly": True, "sourceDate": "2026-09-14", "matchingWindows": []}
    config["results_dir"] = str(tmp_path)
    state = fast.build_state(technical["ticker"], technical["date"], config, technical, review, material)
    assert state["intraday_contract"] == baseline["intraday_contract"]
    assert state["news_report"] == baseline["news_report"]
    assert "이전 후보 가격흐름 참고" in fast.IntradayReport(config).save_reports(state, technical["ticker"]).read_text()
    assert state["intraday_replay"]["candidatePathReference"]["referenceOnly"]


def test_dashboard_shows_and_escapes_candidate_recap(study):
    recap = paths.compact(paths.analyze(study[0], study[1].date()))
    recap["candidates"][0]["name"] = "<script>bad()</script>"
    html = main.render_monitor_html({"postmarketReview": {"candidateReview": recap}})
    assert "후보 가격흐름" in html and "09:21" in html
    assert "&lt;script&gt;bad()&lt;/script&gt;" in html and "<script>bad()</script>" not in html


def test_external_bundle_reuses_complete_prices_and_retries_missing_only(archive, study, monkeypatch):
    local, risk, _ = archive
    inputs, start, _, _ = study
    day = start.date()
    now = start.replace(hour=15, minute=35)
    second = deepcopy(inputs["candidates"][0]["candidates"][0])
    second["ticker"] = "035420.KS"
    inputs["candidates"][0]["candidates"].append(second)
    postmarket.atomic_json(local / "candidates" / f"auto_paper_candidates_{day}_test.json", inputs["candidates"][0])
    backend = Mock()
    backend.clock.return_value = now
    rows = inputs["candidatePrices"]["005930"]["candles"]
    fetch = Mock(side_effect=[rows, TimeoutError(), rows])
    monkeypatch.setattr(paths, "session_candles", fetch)
    first = postmarket.run_review(local, "dry-run", day, risk, now=now, market=inputs["market"], backend=backend)
    assert first["status"] == "partial" and not first["reviewFinished"]
    assert first["candidateReview"]["completeCount"] == 1
    assert Path(first["directory"], "opportunities.json").exists()
    second = postmarket.run_review(local, "dry-run", day, risk, now=now + timedelta(minutes=15), market=inputs["market"], backend=backend)
    assert second["status"] == "completed" and second["reviewFinished"]
    assert fetch.call_count == 3 and second["candidateReview"]["completeCount"] == 2
    assert "후보 가격흐름" in Path(second["reportPath"]).read_text()
    assert paths.prior_reference(local, "dry-run", "paper", "005930.KS", now + timedelta(days=1), study[3])


def test_candidate_fetch_retries_are_persistently_bounded(archive, study, monkeypatch):
    local, risk, _ = archive
    inputs, start, _, _ = study
    day, now = start.date(), start.replace(hour=15, minute=35)
    postmarket.atomic_json(local / "candidates" / f"auto_paper_candidates_{day}_test.json", inputs["candidates"][0])
    backend = Mock()
    backend.clock.return_value = now
    fetch = Mock(side_effect=TimeoutError())
    monkeypatch.setattr(paths, "session_candles", fetch)
    for attempt in range(4):
        record = postmarket.run_review(local, "dry-run", day, risk, now=now + timedelta(minutes=15 * attempt),
                                      market=inputs["market"], backend=backend)
    assert fetch.call_count == 3
    assert record["status"] == "partial" and record["reviewFinished"]

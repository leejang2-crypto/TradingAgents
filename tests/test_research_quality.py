from copy import deepcopy
from datetime import date, timedelta
from unittest.mock import Mock

import pandas as pd
import pytest

from tradingagents.research.long_term import load_market, run_research, screen
from tradingagents.research.quality import apply_quality, build_quarters, compare_peers, evaluate, filters_from, statement_rows
from tests.test_long_term_research import config, market


def test_full_quality_score_and_filter_gating():
    data = market()
    row = screen("005930.KS", data, "3-12")
    apply_quality(row, data, filters_from({}))
    assert row["eligible"] and row["rankingScore"] == 100
    assert row["quality"]["score"] == row["quality"]["coverage"] == 100
    assert sum(f["weight"] for f in row["quality"]["facts"]) == 100
    assert len(row["quality"]["quarters"]) == 8
    apply_quality(row, data, filters_from({"max_pe": 10}))
    assert not row["eligible"] and not row["quality"]["filtersPassed"]


@pytest.mark.parametrize("payload", [{"max_pe": True}, {"max_pe": "NaN"}, {"max_pe": "inf"},
    {"max_pe": 0}, {"min_quality_score": 101}, {"unknown": 10}, [], {"min_turnover_krw": -1}])
def test_invalid_filter_values(payload):
    with pytest.raises(ValueError):
        filters_from(payload)


def test_quarter_adapter_handles_missing_zero_and_future_columns():
    frame = pd.DataFrame({pd.Timestamp("2026-06-30"): [0, float("nan"), 12],
                          pd.Timestamp("2027-06-30"): [100, 20, 12]},
                         index=["Total Revenue", "OperatingIncome", "Net Income"])
    result = statement_rows(frame, "income", today=date(2026, 9, 17))
    assert list(result) == ["2026-06-30"]
    assert result["2026-06-30"] == {"revenue": 0, "operatingIncome": None, "netIncome": 12}
    rows = build_quarters({"income": result})
    assert rows[0]["operatingMargin"] is None


def test_fcf_derived_using_capex_outflow_not_added_back():
    data = market()
    for q in data["statements"]["cash"].values():
        q["freeCashFlow"] = None
    negative = build_quarters(data["statements"])
    for q in data["statements"]["cash"].values():
        q["capex"] = 5
    positive = build_quarters(data["statements"])
    assert all(q["freeCashFlow"] == 15 for q in negative + positive)


def test_insufficient_quarters_never_substituted_with_summary_metrics():
    data = market()
    data["statements"]["income"] = dict(list(data["statements"]["income"].items())[:3])
    result = evaluate(data, filters_from({}))
    assert not any(f["id"] in {"q_growth", "q_cash", "q_fcf", "q_margin"} for f in result["facts"])
    row = screen("005930.KS", data, "3-12")
    apply_quality(row, data, filters_from({}))
    assert not row["eligible"]


def test_annual_periods_and_mismatched_cash_periods_are_not_summed():
    data = market()
    data["statements"]["income"] = {f"{date.today().year - i}-01-01": values for i, values in enumerate(data["statements"]["income"].values())}
    result = evaluate(data, filters_from({}))
    assert not any(f["id"] in {"q_growth", "q_cash", "q_fcf", "q_margin"} for f in result["facts"])
    data = market()
    key = list(data["statements"]["cash"])[0]
    data["statements"]["cash"]["2000-01-01"] = data["statements"]["cash"].pop(key)
    assert not any(f["id"] == "q_cash" for f in evaluate(data, filters_from({}))["facts"])


def test_positive_income_with_negative_cash_is_a_hard_exclusion():
    data = market()
    for q in data["statements"]["cash"].values():
        q["operatingCashFlow"] = -20
    row = screen("005930.KS", data, "3-12")
    apply_quality(row, data, filters_from({"min_quality_score": 0}))
    assert not row["eligible"] and any("순이익" in warning for warning in row["quality"]["risks"])


def test_negative_equity_excluded_even_when_roe_looks_positive():
    data = market()
    next(iter(data["statements"]["balance"].values()))["equity"] = -10
    row = screen("005930.KS", data, "3-12")
    apply_quality(row, data, filters_from({}))
    assert not row["eligible"]


def test_share_increase_checked_but_split_is_not_called_dilution():
    data = market()
    next(iter(data["statements"]["balance"].values()))["shares"] = 200
    result = evaluate(data, filters_from({}))
    assert any("주식 수 증가율" in risk for risk in result["risks"])
    data["splitDates"] = [list(data["statements"]["balance"])[1]]
    result = evaluate(data, filters_from({}))
    assert not any(f["id"] == "q_dilution" for f in result["facts"])
    assert not any("주식 수 증가율" in risk for risk in result["risks"])
    assert any("분할" in text for text in result["missing"])


def test_drawdown_and_turnover_limits_block_candidates():
    data = market()
    data["closes"] = [100] * 280 + [40] * 20
    result = evaluate(data, filters_from({}))
    assert result["maxDrawdownPct"] == 60
    assert result["risks"]
    data = market()
    data["averageTurnover20"] = None
    assert any("거래대금 부족" in risk for risk in evaluate(data, filters_from({}))["risks"])


def test_invalid_and_stale_quarters_do_not_get_quality_points():
    data = market()
    data["statements"] = {kind: {"2000-01-01": next(iter(rows.values()))} for kind, rows in data["statements"].items()}
    result = evaluate(data, filters_from({}))
    assert result["coverage"] == 10 and result["score"] == 10


def test_peer_comparison_excludes_self_wrong_industry_and_stale_issuers():
    rows = []
    for ticker, pe, industry in [("005930.KS", 10, "Semiconductors"), ("000660.KS", 20, "Semiconductors"),
                                 ("039030.KQ", 30, "Semiconductors"), ("005380.KS", 100, "Auto")]:
        data = market(trailingPE=pe, industry=industry)
        row = screen(ticker, data, "3-12")
        apply_quality(row, data, filters_from({}))
        rows.append(row)
    compare_peers(rows)
    fact = next(f for f in rows[0]["peerComparison"]["facts"] if f["id"] == "peer_PER")
    assert fact["peers"] == ["000660.KS", "039030.KQ"] and "25.00" in fact["text"]
    assert not rows[-1]["peerComparison"]["facts"]
    rows[2]["warnings"] = ["stale"]
    compare_peers(rows)
    assert not rows[0]["peerComparison"]["facts"]


def test_missing_new_data_blocks_shortlist_and_llm_but_keeps_base_result():
    data = market()
    data.pop("statements")
    review = Mock()
    result = run_research(config(provider="openai"), fetch=lambda _: deepcopy(data), review=review)
    assert result["rows"][0]["score"] == 100
    assert not result["shortlist"] and result["rows"][0]["quality"]["missing"]
    assert result["scoringVersion"] == "long-term-v2"
    review.assert_not_called()


def test_loader_fetches_quarterly_statements_and_unadjusted_turnover(monkeypatch):
    import yfinance
    prices = pd.DataFrame({"Adj Close": [90.] * 300, "Close": [100.] * 300,
                           "Volume": [1000.] * 300, "Stock Splits": [0.] * 299 + [2.]},
                          index=pd.date_range(end=date.today(), periods=300))
    stock = Mock()
    stock.get_info.return_value = market()["info"]
    stock.history.return_value = prices
    stock.get_income_stmt.return_value = pd.DataFrame({pd.Timestamp(date.today() - timedelta(days=20)): [100]}, index=["TotalRevenue"])
    stock.get_cash_flow.side_effect = OSError("secret")
    stock.get_balance_sheet.return_value = pd.DataFrame()
    monkeypatch.setattr(yfinance, "Ticker", lambda _: stock)
    data = load_market("005930.KS")
    assert data["closes"][-1] == 90 and data["averageTurnover20"] == 100000
    assert data["statementErrors"] == ["cash 분기 자료 조회 실패 (OSError)"]
    assert data["splitDates"] == [date.today().isoformat()]
    stock.get_income_stmt.assert_called_once_with(freq="quarterly")
    stock.get_balance_sheet.assert_called_once_with(freq="quarterly")
    assert stock.history.call_args.kwargs["auto_adjust"] is False

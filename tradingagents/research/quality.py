"""Deterministic, period-aligned evidence for long-term research only."""

from datetime import date
import math
import re
from statistics import median

FILTERS = {
    "max_pe": (25, 1, 200),
    "min_revenue_growth_pct": (5, -50, 100),
    "max_debt_equity": (100, 0, 1000),
    "max_drawdown_pct": (45, 1, 100),
    "max_dilution_pct": (5, 0, 100),
    "min_turnover_krw": (100_000_000, 0, 1_000_000_000_000),
    "min_quality_score": (60, 0, 100),
}
FIELDS = {
    "income": {"revenue": "TotalRevenue", "operatingIncome": "OperatingIncome", "netIncome": "NetIncome"},
    "cash": {"operatingCashFlow": "OperatingCashFlow", "capex": "CapitalExpenditure", "freeCashFlow": "FreeCashFlow"},
    "balance": {"currentAssets": "CurrentAssets", "currentLiabilities": "CurrentLiabilities",
                "equity": "StockholdersEquity", "shares": "OrdinarySharesNumber"},
}


def finite(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError):
        return None


def filters_from(payload):
    if payload is None:
        payload = {}
    if not isinstance(payload, dict) or set(payload) - FILTERS.keys():
        raise ValueError("중장기 선별 조건이 올바르지 않습니다.")
    result = {}
    for key, (default, low, high) in FILTERS.items():
        value = finite(payload.get(key, default))
        if value is None or not low <= value <= high:
            raise ValueError(f"{key}: {low}~{high} 범위의 숫자가 필요합니다.")
        result[key] = value
    return result


def statement_rows(frame, kind, *, today=None):
    """Normalize provider quarterly columns, never sum annual/YTD data as quarters."""
    today = today or date.today()
    normalize = lambda value: re.sub(r"[^a-z]", "", str(value).lower())
    indices = {normalize(label): label for label in frame.index}
    result = {}
    for column in frame.columns:
        try:
            day = date.fromisoformat(str(column)[:10])
        except ValueError:
            continue
        if day > today:
            continue
        values = {}
        for key, label in FIELDS[kind].items():
            index = indices.get(normalize(label))
            values[key] = finite(frame.loc[index, column]) if index is not None else None
        if any(value is not None for value in values.values()):
            result[day.isoformat()] = values
    return dict(sorted(result.items(), reverse=True)[:8])


def build_quarters(statements):
    # Anchor every comparison to actual income-statement quarter ends.
    result = []
    for day, income in sorted(statements.get("income", {}).items(), reverse=True)[:8]:
        row = {"date": day, **income, **statements.get("cash", {}).get(day, {}),
               **statements.get("balance", {}).get(day, {})}
        if row.get("freeCashFlow") is None and row.get("operatingCashFlow") is not None and row.get("capex") is not None:
            row["freeCashFlow"] = row["operatingCashFlow"] - abs(row["capex"])
        revenue, income_value = row.get("revenue"), row.get("operatingIncome")
        row["operatingMargin"] = income_value / revenue if revenue is not None and revenue > 0 and income_value is not None else None
        result.append(row)
    return result


def consecutive(rows):
    return all(70 <= (date.fromisoformat(a["date"]) - date.fromisoformat(b["date"])).days <= 110
               for a, b in zip(rows, rows[1:]))


def evaluate(market, filters, *, today=None):
    today = today or date.today()
    quarters = build_quarters(market.get("statements", {}))
    facts, missing, risks = [], [], []
    score = coverage = 0
    fresh = bool(quarters and 0 <= (today - date.fromisoformat(quarters[0]["date"])).days <= 200)
    q4 = quarters[:4]
    usable4 = fresh and len(q4) == 4 and consecutive(q4)
    yoy = []
    for index in range(min(4, max(0, len(quarters) - 4))):
        current, prior = quarters[index], quarters[index + 4]
        span = (date.fromisoformat(current["date"]) - date.fromisoformat(prior["date"])).days
        a, b = finite(current.get("revenue")), finite(prior.get("revenue"))
        value = a / b - 1 if a is not None and a >= 0 and b is not None and b > 0 and 330 <= span <= 400 else None
        yoy.append(value)
        current["revenueYoY"] = value

    def add(key, label, weight, known, passed=False, text=""):
        nonlocal score, coverage
        if not known:
            missing.append(label)
            return
        coverage += weight
        score += weight if passed else 0
        facts.append({"id": key, "text": label + ": " + text, "passed": bool(passed),
                      "points": weight if passed else 0, "weight": weight,
                      "source": "Yahoo Finance 분기 재무·가격 / Python 계산"})

    growth_known = fresh and len(yoy) >= 3 and all(v is not None for v in yoy[:3]) and consecutive(quarters[:7])
    latest_growth = yoy[0] if fresh and yoy else None
    add("q_growth", "최근 3분기 연속 전년동기 매출 성장", 20, growth_known,
        growth_known and all(v > 0 for v in yoy[:3]) and latest_growth * 100 > filters["min_revenue_growth_pct"],
        ", ".join(f"{v * 100:.2f}%" for v in yoy[:3] if v is not None) + f" (최근분기 > {filters['min_revenue_growth_pct']:g}%)")
    margins = [q.get("operatingMargin") for q in quarters[:5]]
    margin_known = fresh and len(margins) == 5 and all(v is not None for v in margins) and consecutive(quarters[:5])
    add("q_margin", "영업이익률 지속성", 15, margin_known,
        margin_known and all(v > 0 for v in margins[:4]) and margins[0] >= margins[4],
        f"최근 {margins[0] * 100:.2f}% / 전년동기 {margins[4] * 100:.2f}%" if margin_known else "")
    def total(key):
        values = [finite(q.get(key)) for q in q4]
        return sum(values) if usable4 and all(v is not None for v in values) else None
    ocf, income, fcf = total("operatingCashFlow"), total("netIncome"), total("freeCashFlow")
    cash_known = ocf is not None and income is not None
    cash_ratio = ocf / income if cash_known and income > 0 else None
    add("q_cash", "최근 4분기 이익의 현금 전환", 20, cash_known,
        cash_known and income > 0 and ocf > 0 and cash_ratio >= 1,
        f"영업현금흐름 {ocf:,.0f} / 순이익 {income:,.0f}" if cash_known else "")
    if cash_known and income > 0 and ocf <= 0:
        risks.append("순이익은 양수이나 최근 4분기 영업현금흐름이 0 이하입니다.")
    fcf_positive = sum(1 for q in q4 if (finite(q.get("freeCashFlow")) or 0) > 0)
    add("q_fcf", "잉여현금흐름 지속성", 15, fcf is not None,
        fcf is not None and fcf > 0 and fcf_positive >= 3,
        f"최근 4분기 합계 {fcf:,.0f} / 양수 {fcf_positive}/4분기" if fcf is not None else "")
    latest = quarters[0] if quarters else {}
    equity, assets, liabilities = [finite(latest.get(k)) for k in ("equity", "currentAssets", "currentLiabilities")]
    balance_known = fresh and equity is not None and assets is not None and assets >= 0 and liabilities is not None and liabilities >= 0
    add("q_liquidity", "단기 유동성·자기자본", 10, balance_known,
        balance_known and equity > 0 and assets >= liabilities,
        f"유동자산 {assets:,.0f} / 유동부채 {liabilities:,.0f} / 자기자본 {equity:,.0f}" if balance_known else "")
    if fresh and equity is not None and equity <= 0:
        risks.append("최근 분기 자기자본이 0 이하입니다.")
    shares = finite(latest.get("shares"))
    previous_shares = finite(quarters[4].get("shares")) if len(quarters) >= 5 else None
    splits = market.get("splitDates", [])
    split_in_window = any(quarters[4]["date"] <= day <= latest["date"] for day in splits) if len(quarters) >= 5 else False
    dilution_known = fresh and len(quarters) >= 5 and consecutive(quarters[:5]) and shares is not None and shares > 0 and previous_shares is not None and previous_shares > 0 and not split_in_window
    dilution = (shares / previous_shares - 1) * 100 if dilution_known else None
    add("q_dilution", "전년동기 대비 보통주 수 증가", 10, dilution_known,
        dilution_known and dilution <= filters["max_dilution_pct"],
        f"{dilution:.2f}% (한도 {filters['max_dilution_pct']:g}%)" if dilution_known else "")
    if dilution_known and dilution > filters["max_dilution_pct"]:
        risks.append("확인된 주식 수 증가율이 설정한 한도를 초과했습니다.")
    if split_in_window:
        missing.append("주식분할/병합 구간의 주식 수 비교는 제외했습니다.")
    closes = [finite(v) for v in market.get("closes", [])[-252:]]
    price_known = len(closes) == 252 and all(v is not None and v > 0 for v in closes)
    drawdown = None
    if price_known:
        peak, drawdown = closes[0], 0
        for close in closes:
            peak = max(peak, close)
            drawdown = max(drawdown, (1 - close / peak) * 100)
    turnover = finite(market.get("averageTurnover20"))
    risk_known = price_known and turnover is not None and turnover >= 0
    add("q_market_risk", "252거래일 최대낙폭·20일 평균 거래대금", 10, risk_known,
        risk_known and drawdown <= filters["max_drawdown_pct"] and turnover >= filters["min_turnover_krw"],
        f"낙폭 {drawdown:.2f}% / 거래대금 {turnover:,.0f} KRW" if risk_known else "")
    if risk_known and not facts[-1]["passed"]:
        risks.append("낙폭 또는 거래대금이 설정한 위험 한도를 벗어났습니다.")
    elif not risk_known:
        risks.append("252거래일 가격 또는 20일 거래대금 부족으로 위험 한도를 확인할 수 없습니다.")
    if not fresh:
        missing.append("최근 200일 이내 분기 재무 자료")
    return {"score": score, "coverage": coverage, "facts": facts, "missing": missing,
            "risks": risks, "quarters": quarters, "latestRevenueGrowth": latest_growth,
            "maxDrawdownPct": drawdown, "averageTurnover20": turnover,
            "errors": market.get("statementErrors", [])}


def apply_quality(row, market, filters, *, today=None):
    quality = evaluate(market, filters, today=today)
    row["quality"] = quality
    row["facts"].extend(quality["facts"])
    row["rankingScore"] = round(row["score"] * .6 + quality["score"] * .4, 2)
    info = market["info"]
    pe, debt = finite(info.get("trailingPE")), finite(info.get("debtToEquity"))
    growth = quality["latestRevenueGrowth"]
    filters_pass = pe is not None and 0 < pe <= filters["max_pe"] and debt is not None and 0 <= debt <= filters["max_debt_equity"] and growth is not None and growth * 100 > filters["min_revenue_growth_pct"]
    quality["filtersPassed"] = filters_pass
    if not filters_pass:
        quality["risks"].append("PER·부채비율·분기 매출 성장률의 필수 조건 미충족 또는 자료 누락")
    row["eligible"] = bool(row["eligible"] and filters_pass and quality["coverage"] >= 70 and quality["score"] >= filters["min_quality_score"] and not quality["risks"])
    row["classification"] = "검토 후보" if row["eligible"] else "추가 확인"
    row["industry"] = info.get("industry")
    row["peerValues"] = {"PER": pe, "PBR": finite(info.get("priceToBook")), "ROE": finite(info.get("returnOnEquity"))}


def compare_peers(rows):
    """Peers are other selected, fresh issuers of the same industry and currency."""
    for row in rows:
        row["facts"] = [fact for fact in row["facts"] if not fact["id"].startswith("peer_")]
        peers = [other for other in rows if other is not row and row.get("industry")
                 and other.get("industry") == row["industry"] and row.get("currency")
                 and other.get("currency") == row["currency"] and not other["warnings"]
                 and row.get("financialDate") and other.get("financialDate")
                 and abs((date.fromisoformat(row["financialDate"]) - date.fromisoformat(other["financialDate"])).days) <= 45]
        comparisons = []
        for key, value in row["peerValues"].items():
            usable = [p for p in peers if p["peerValues"][key] is not None and (key == "ROE" or p["peerValues"][key] > 0)]
            if len(usable) < 2 or value is None or (key != "ROE" and value <= 0) or row["warnings"]:
                continue
            middle = median(p["peerValues"][key] for p in usable)
            unit = "%" if key == "ROE" else "배"
            scale = 100 if key == "ROE" else 1
            item = {"id": "peer_" + key, "text": f"동일 업종 {key}: {value * scale:.2f}{unit} / 다른 {len(usable)}개사 중앙값 {middle * scale:.2f}{unit}",
                    "passed": value >= middle if key == "ROE" else value <= middle,
                    "points": 0, "weight": 0, "source": "선택한 종목 중 Yahoo 업종 분류 비교",
                    "peers": [p["ticker"] for p in usable]}
            comparisons.append(item)
        row["peerComparison"] = {"industry": row.get("industry"), "facts": comparisons,
                                 "note": "전체 업종 순위가 아닌 입력 종목 내 비교이며 점수에는 반영하지 않습니다." if comparisons else "동일 업종·통화·유사 기준일의 비교 종목이 지표별 2개 이상 필요합니다."}
        row["facts"].extend(comparisons)

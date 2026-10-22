"""Deterministic entry filters. Toss 1m timestamps denote candle END times.

No LLM calls. VWAP/turnover are OHLCV estimates, not tick-exact values.
Only complete regular-session bars are accepted; missing data blocks entry.
"""

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
import time

import pandas as pd

from .paper_account import KST, number


INTRADAY_DEFAULTS = {
    "position_target_return": "0.01",
    "position_stop_loss": "0.005",
    "daily_loss_limit": "0.02",
    "trade_risk_fraction": "0.002",
    "estimated_cost_rate": "0.003",
    "min_reward_risk": "1.0",
    "min_relative_volume": "1.5",
    "min_session_turnover": "1000000000",
    "max_spread": "0.002",
    "market_drop_limit": "0.01",
    "max_entry_extension": "0.003",
    "stagnation_minutes": 90,
    "max_consecutive_stops": 3,
    "entry_strategy": "breakout",
    "opening_range_minutes": 15,
    "entry_watch_minutes": 30,
}
ENTRY_STRATEGIES = {"breakout": "직전 15분 고점 돌파", "opening_range": "장 초반 범위 돌파",
                    "pullback": "VWAP 눌림목 회복", "vwap_reclaim": "VWAP 재돌파"}
DECIMAL_LIMITS = {
    "position_target_return": ("0.001", "0.2"),
    "position_stop_loss": ("0.001", "0.1"),
    "daily_loss_limit": ("0.001", "0.1"),
    "trade_risk_fraction": ("0.0001", "0.02"),
    "estimated_cost_rate": ("0", "0.05"),
    "min_reward_risk": ("0.1", "10"),
    "min_relative_volume": ("1", "10"),
    "min_session_turnover": ("1000000", "1000000000000"),
    "max_spread": ("0.0001", "0.01"),
    "market_drop_limit": ("0.001", "0.1"),
    "max_entry_extension": ("0.0001", "0.02"),
}
INTEGER_LIMITS = {"stagnation_minutes": (15, 360), "max_consecutive_stops": (1, 10),
                  "opening_range_minutes": (5, 60), "entry_watch_minutes": (0, 120)}


def stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Missing data timezone")
    return parsed.astimezone(KST)


class CandleDataError(ValueError):
    def __init__(self, diagnostics):
        self.diagnostics = dict(diagnostics)
        d = self.diagnostics
        label = {"too-early": "최소 완성 분봉 대기", "missing": "분봉 누락",
                 "unaligned": "분봉 시각 불일치"}[d["code"]]
        super().__init__(
            f"{d.get('symbol', '시세')} {label}: 필요 {d['expectedCount']}개 / 수신 {d['receivedCount']}개, "
            f"최소 {d['minimumMinutes']}분, 최초 {d['firstAt'] or '-'}, 최종 {d['lastAt'] or '-'}, "
            f"누락 {d['missingCount']}개 [{', '.join(d['missingAt'][:5])}], "
            f"비정상 시각 {d['unexpectedCount']}개, 조회 {d.get('pageCount', 0)}회"
        )


def candle_coverage(timestamps, start, now, minimum_minutes=20, *, allow_opening_gap=False):
    end = start + timedelta(minutes=int((now - start).total_seconds() // 300) * 5)
    expected = set(pd.date_range(start + timedelta(minutes=1), end, freq="min"))
    actual = {at for at in timestamps if start < at <= end}
    missing, unexpected = sorted(expected - actual), sorted(actual - expected)
    opening_gap = (allow_opening_gap and len(expected) >= minimum_minutes and not unexpected
                   and missing == [start + timedelta(minutes=1)])
    return {"code": "too-early" if len(expected) < minimum_minutes else "unaligned" if unexpected else "opening-gap" if opening_gap else "missing" if missing else "complete",
            "acceptedOpeningGap": opening_gap,
            "sessionStart": start.isoformat(), "asOf": now.isoformat(), "expectedEnd": end.isoformat(),
            "minimumMinutes": minimum_minutes, "expectedCount": len(expected), "receivedCount": len(actual),
            "firstAt": min(actual).isoformat() if actual else None, "lastAt": max(actual).isoformat() if actual else None,
            "missingCount": len(missing), "missingAt": [at.isoformat() for at in missing],
            "unexpectedCount": len(unexpected), "unexpectedAt": [at.isoformat() for at in unexpected]}


def session_candles(call, symbol, start, now, *, index=False, diagnostics=None):
    """Fetch to the opening boundary, then repair gaps using only actual API bars."""
    result, cursor, seen = {}, now.isoformat(), set()
    info = diagnostics if diagnostics is not None else {}
    info.update(symbol=symbol, timestampKind="start" if index else "end", pageCount=0, retryCount=0, pages=[])
    offset = timedelta(minutes=1) if index else timedelta(0)
    end = stamp(candle_coverage([], start, now)["expectedEnd"])

    def coverage():
        info.update(candle_coverage(result, start, now))
        return info["code"] == "complete"

    def fetch(before):
        from tradingagents.monitoring.control import checkpoint
        checkpoint()
        data = call("get_candles", symbol, before=before, index=index)
        rows = data.get("candles")
        if not isinstance(rows, list):
            raise ValueError(f"{symbol}: Invalid candles response")
        raw_times = [stamp(row["timestamp"]) for row in rows]
        info["pageCount"] += 1
        info["pages"].append({"before": before, "count": len(rows),
            "firstAt": min(raw_times).isoformat() if raw_times else None,
            "lastAt": max(raw_times).isoformat() if raw_times else None,
            "nextBefore": data.get("nextBefore")})
        for row, raw_at in zip(rows, raw_times):
            # Indicator bars start at timestamp; stock bars end at timestamp.
            at = raw_at + offset
            if start < at <= end:
                normalized = {**row, "timestamp": at.isoformat()}
                if at in result and result[at] != normalized:
                    raise ValueError(f"{symbol}: Conflicting duplicate candle at {at.isoformat()}")
                result[at] = normalized
        coverage()
        return data, raw_times

    coverage()
    if info["code"] == "too-early":
        raise CandleDataError(info)
    # Some responses contain fewer than the requested 200 bars. Never assume page size.
    for _ in range(12):
        seen.add(cursor)
        data, raw_times = fetch(cursor)
        if coverage():
            info["paginationStop"] = "complete"
            break
        if not raw_times or min(raw_times) + offset <= start + timedelta(minutes=1):
            info["paginationStop"] = "opening-or-empty"
            break
        next_cursor = data.get("nextBefore")
        if not next_cursor:
            info["paginationStop"] = "no-next-page"
            break
        if next_cursor in seen or stamp(next_cursor) >= stamp(cursor):
            info["paginationStop"] = "cursor-not-moving"
            break
        cursor = next_cursor
    else:
        info["paginationStop"] = "page-limit"
    if not coverage() and info["code"] == "missing":
        missing = [stamp(at) for at in info["missingAt"]]
        tail = len(missing) <= 2 and missing[-1] == end and missing[0] >= end - timedelta(minutes=1)
        attempts = 2 if tail and (now - end).total_seconds() < 30 else 1
        for _ in range(attempts):
            if tail:
                time.sleep(1)
            # Query just beyond the last missing raw timestamp; never fabricate a bar.
            before = min(now, stamp(info["missingAt"][-1]) - offset + timedelta(minutes=1))
            info["retryCount"] += 1
            fetch(before.isoformat())
            if coverage() or info["code"] != "missing":
                break
    if not coverage():
        if index and symbol in {"KOSPI", "KOSDAQ"}:
            info.update(candle_coverage(result, start, now, allow_opening_gap=True))
        if info["code"] != "opening-gap":
            raise CandleDataError(info)
        info["warning"] = "지수 개장 첫 1분 누락 허용: 첫 실제 관측 봉 기준, 첫 1분 변동 미반영"
    return list(result.values())


def _minute_frame(rows, start, now, *, index=False, minimum_minutes=20):
    # Discard the forming 5m bar, but never fill missing minutes with invented prices.
    end = start + timedelta(minutes=int((now - start).total_seconds() // 300) * 5)
    values = {}
    for row in rows:
        at = stamp(row["timestamp"])
        if not start < at <= end:
            continue
        if not index and row.get("currency") != "KRW":
            raise ValueError("Non-KRW candle")
        o, h, low, c, volume = [number(row[key]) for key in ("openPrice", "highPrice", "lowPrice", "closePrice", "volume")]
        if min(o, h, low, c) <= 0 or volume < 0 or not low <= min(o, c) <= max(o, c) <= h:
            raise ValueError("Invalid OHLCV")
        current = [float(v) for v in (o, h, low, c, volume)]
        if at in values and values[at] != current:
            raise ValueError("Conflicting duplicate candle")
        values[at] = current
    coverage = candle_coverage(values, start, now, minimum_minutes, allow_opening_gap=index)
    if coverage["code"] not in {"complete", "opening-gap"}:
        raise CandleDataError(coverage)
    frame = pd.DataFrame.from_dict(values, orient="index", columns=["open", "high", "low", "close", "volume"]).sort_index()
    return frame, coverage


def market_reference(rows, start, now):
    """Index return from actual minute OHLC, never a fabricated opening bar."""
    frame, coverage = _minute_frame(rows, start, now, index=True)
    basis = frame.index[0].to_pydatetime() - timedelta(minutes=1)
    partial = coverage["acceptedOpeningGap"]
    return {"return": float(frame.close.iloc[-1] / frame.open.iloc[0] - 1),
            "basisStart": basis.isoformat(), "firstBarEnd": frame.index[0].isoformat(),
            "barEnd": frame.index[-1].isoformat(), "openingGap": partial,
            "warning": (f"지수 개장 첫 1분 누락: {basis.isoformat()} 첫 실제 관측 봉 시가 기준, 첫 1분 변동 미반영" if partial else None)}


def bars(rows, start, now, *, index=False, minimum_minutes=20):
    frame, coverage = _minute_frame(rows, start, now, index=index, minimum_minutes=minimum_minutes)
    typical = (frame.high + frame.low + frame.close) / 3
    turnover = float((typical * frame.volume).sum())
    volume = float(frame.volume.sum())
    if not index and volume <= 0:
        raise ValueError("No session volume")
    five = frame.resample("5min", origin=start, closed="right", label="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
    if coverage["acceptedOpeningGap"]:
        five = five.iloc[1:]  # Never present the incomplete opening group as a complete 5m bar.
    return five, turnover, turnover / volume if volume else 0


def evaluate_entry(symbol, rows, book, markets, start, now, settings):
    cfg = {**INTRADAY_DEFAULTS, **settings}
    five, turnover, vwap = bars(rows, start, now)
    at = stamp(book["timestamp"])
    if book.get("currency") != "KRW" or not 0 <= (now - at).total_seconds() <= 30:
        raise ValueError("Stale or invalid orderbook")
    asks = [number(r["price"]) for r in book["asks"] if number(r["volume"]) > 0]
    bids = [number(r["price"]) for r in book["bids"] if number(r["volume"]) > 0]
    ask, bid = min(asks), max(bids)
    if not 0 < bid <= ask or ask != ask.to_integral_value():
        raise ValueError("Invalid KR orderbook prices")
    spread = (ask - bid) / ask
    last, prior = five.iloc[-1], five.iloc[-2]
    average_volume = five.volume.iloc[max(0, len(five) - 7):-1].mean()
    relative = float(last.volume / average_volume) if average_volume > 0 else 0
    previous_close = five.close.shift(1)
    tr = pd.concat([five.high - five.low, (five.high - previous_close).abs(), (five.low - previous_close).abs()], axis=1).max(axis=1)
    atr = number(str(tr.tail(14).mean()))
    returns, references, warnings = {}, {}, []
    for market in ("KOSPI", "KOSDAQ"):
        references[market] = market_reference(markets[market], start, now)
        returns[market] = references[market]["return"]
        if references[market]["warning"]:
            warnings.append(market + ": " + references[market]["warning"])
    reasons = []
    def require(condition, reason):
        if not condition:
            reasons.append(reason)
    require(spread <= number(cfg["max_spread"]), "호가 스프레드 초과")
    require(turnover >= float(cfg["min_session_turnover"]), "당일 추정 거래대금 부족")
    require(relative >= float(cfg["min_relative_volume"]), "최근 5분봉 대비 거래량 부족")
    require(min(returns.values()) > -float(cfg["market_drop_limit"]), "시장 지수 하락 제한")
    require(last.close > vwap and bid > number(str(vwap)), "당일 추정 VWAP 하회")
    require(abs(ask / number(str(last.close)) - 1) <= number(cfg["max_entry_extension"]), "신호 가격 이탈 / 추격 매수 차단")
    if cfg["entry_strategy"] == "breakout":
        require(last.close > five.high.iloc[-4:-1].max(), "직전 15분 고점 돌파 미확인")
    elif cfg["entry_strategy"] == "opening_range":
        minutes = int(cfg["opening_range_minutes"])
        if minutes < 5 or minutes > 60 or minutes % 5:
            raise ValueError("Opening range must be 5-60 minutes in 5-minute steps")
        opening_end = start + timedelta(minutes=minutes)
        opening = five.loc[five.index <= opening_end]
        require(five.index[-1] > opening_end, "장 초반 범위 형성 중")
        require(last.close > opening.high.max() and prior.close <= opening.high.max(),
                "장 초반 고점의 신규 돌파 미확인")
    elif cfg["entry_strategy"] == "pullback":
        require(last.low <= vwap <= last.close and last.close > prior.close and last.close > last.open,
                "VWAP 눌림목 회복 미확인")
    elif cfg["entry_strategy"] == "vwap_reclaim":
        # Compare each close against the VWAP known at that bar, not today's later VWAP.
        previous_rows = [row for row in rows if stamp(row["timestamp"]) <= five.index[-2]]
        _, _, previous_vwap = bars(previous_rows, start, five.index[-2].to_pydatetime(), minimum_minutes=15)
        require(prior.close <= previous_vwap and last.close > vwap and last.close > last.open,
                "VWAP 하단에서 상단으로의 재돌파 미확인")
    else:
        raise ValueError("Unknown entry strategy")
    # ATR/previous-low stop is capped at the configured maximum loss distance.
    stop = max(ask * (1 - number(cfg["position_stop_loss"])), ask - atr, number(str(prior.low)))
    require(stop < bid, "손절선이 현재 매수호가 이상")
    distance = (ask - stop) / ask
    cost = number(cfg["estimated_cost_rate"])
    reward = number(cfg["position_target_return"]) - cost
    ratio = reward / (max(Decimal(0), distance) + cost) if distance + cost > 0 else Decimal(0)
    require(ratio >= number(cfg["min_reward_risk"]), "예상 비용 반영 손익비 부족")
    return {"allowed": not reasons, "reasons": reasons, "symbol": symbol, "at": now.isoformat(),
            "barEnd": five.index[-1].isoformat(), "ask": str(ask), "bid": str(bid),
            "stopPrice": str(stop), "stopFraction": str(distance), "atr": str(atr),
            "vwapEstimate": vwap, "turnoverEstimate": turnover, "relativeVolume": relative,
            "spread": str(spread), "marketReturns": returns, "marketReferences": references,
            "warnings": warnings, "rewardRisk": str(ratio), "strategy": cfg["entry_strategy"]}


def size_entry(plan, signal, settings):
    cfg = {**INTRADAY_DEFAULTS, **settings}
    price = number(signal["ask"])
    cost = number(cfg["estimated_cost_rate"])
    capital = number(cfg.get("paper_capital", "10000000"))
    budget = capital * number(cfg.get("position_pct", ".10"))
    if "available_capital" in cfg:
        budget = min(budget, max(Decimal(0), number(cfg["available_capital"])))
    if plan.order_amount:
        budget = min(budget, number(plan.order_amount))
    risk_per_share = price * (max(Decimal(0), number(signal["stopFraction"])) + cost)
    if price <= 0 or risk_per_share <= 0:
        raise ValueError("Invalid entry risk")
    risk_budget = capital * number(cfg["trade_risk_fraction"])
    if "available_risk" in cfg:
        risk_budget = min(risk_budget, max(Decimal(0), number(cfg["available_risk"])))
    qty = min(budget / (price * (1 + cost)), risk_budget / risk_per_share)
    if plan.quantity:
        qty = min(qty, number(plan.quantity))
    return qty.to_integral_value(rounding=ROUND_DOWN)

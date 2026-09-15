"""Deterministic entry filters. Toss 1m timestamps denote candle END times.

No LLM calls. VWAP/turnover are OHLCV estimates, not tick-exact values.
Only complete regular-session bars are accepted; missing data blocks entry.
"""

from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN

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
}
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
INTEGER_LIMITS = {"stagnation_minutes": (15, 360), "max_consecutive_stops": (1, 10)}


def stamp(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Missing data timezone")
    return parsed.astimezone(KST)


def session_candles(call, symbol, start, now, *, index=False):
    """Bounded pagination, including the opening minute even for afternoon runs."""
    result, cursor, seen = {}, now.isoformat(), set()
    for _ in range(3):
        data = call("get_candles", symbol, before=cursor, index=index)
        rows = data.get("candles")
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            # Stock candles use end times; indicator candles use START times.
            at = stamp(row["timestamp"]) + (timedelta(minutes=1) if index else timedelta(0))
            if start < at <= now:
                normalized = {**row, "timestamp": at.isoformat()}
                if at in result and result[at] != normalized:
                    raise ValueError("Conflicting duplicate candle")
                result[at] = normalized
        if min(stamp(r["timestamp"]) for r in rows) <= start + (timedelta(0) if index else timedelta(minutes=1)):
            break
        cursor = data.get("nextBefore")
        if not cursor or cursor in seen:
            break
        seen.add(cursor)
    return list(result.values())


def bars(rows, start, now, *, index=False):
    # Discard the forming 5m bar, but never fill missing minutes with invented prices.
    end = start + timedelta(minutes=int((now - start).total_seconds() // 300) * 5)
    expected = pd.date_range(start + timedelta(minutes=1), end, freq="min")
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
    if len(expected) < 20 or set(values) != set(expected):
        raise ValueError("Opening data or completed 5m bars missing (minimum 20 minutes)")
    frame = pd.DataFrame.from_dict(values, orient="index", columns=["open", "high", "low", "close", "volume"]).sort_index()
    typical = (frame.high + frame.low + frame.close) / 3
    turnover = float((typical * frame.volume).sum())
    volume = float(frame.volume.sum())
    if not index and volume <= 0:
        raise ValueError("No session volume")
    five = frame.resample("5min", origin=start, closed="right", label="right").agg(
        {"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
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
    returns = {}
    for market in ("KOSPI", "KOSDAQ"):
        index_bars, _, _ = bars(markets[market], start, now, index=True)
        returns[market] = float(index_bars.close.iloc[-1] / index_bars.open.iloc[0] - 1)
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
    elif cfg["entry_strategy"] == "pullback":
        require(last.low <= vwap <= last.close and last.close > prior.close and last.close > last.open,
                "VWAP 눌림목 회복 미확인")
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
            "spread": str(spread), "marketReturns": returns, "rewardRisk": str(ratio), "strategy": cfg["entry_strategy"]}


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

"""Read-only intraday shortlist. Scores are heuristics, not return probabilities."""

from datetime import datetime, timedelta
import math

import pandas as pd

from .intraday import INTRADAY_DEFAULTS, bars, session_candles, stamp, market_reference
from .paper_account import now_kst
from tradingagents.monitoring.control import checkpoint, RunStopped


def measure_candidate(rows, start, now, market_return, settings, *, reference_start=None):
    cfg = {**INTRADAY_DEFAULTS, **settings}
    five, turnover, vwap = bars(rows, start, now)
    close = float(five.close.iloc[-1])
    change = close / float(five.open.iloc[0]) - 1
    average = float(five.volume.iloc[-7:-1].mean())
    relative = float(five.volume.iloc[-1]) / average if average > 0 else 0
    previous = five.close.shift(1)
    atr = float(pd.concat([five.high - five.low, (five.high - previous).abs(),
                          (five.low - previous).abs()], axis=1).max(axis=1).tail(14).mean()) / close
    reference_start = start if reference_start is None else reference_start
    if reference_start not in {start, start + timedelta(minutes=1)}:
        raise ValueError("시장 비교 기준 시각 불일치")
    reference_row = next(row for row in rows if stamp(row["timestamp"]) == reference_start + timedelta(minutes=1))
    aligned_return = close / float(reference_row["openPrice"]) - 1
    strength = aligned_return - market_return
    metrics = {"turnoverEstimate": turnover, "relativeVolume": relative,
               "sessionReturn": change, "relativeStrength": strength,
               "comparisonReturn": aligned_return, "comparisonStart": reference_start.isoformat(),
               "atrFraction": atr, "vwapEstimate": vwap, "close": close,
               "barEnd": five.index[-1].isoformat()}
    if not all(math.isfinite(value) for value in metrics.values() if isinstance(value, (float, int))):
        raise ValueError("유효하지 않은 시장 지표")
    reasons = []
    if turnover < float(cfg["min_session_turnover"]): reasons.append("당일 추정 거래대금 부족")
    if relative < 1: reasons.append("최근 거래량이 직전 평균 미달")
    if change <= 0 or strength <= 0: reasons.append("시초가 대비 상승/시장 대비 강도 부족")
    if close <= vwap: reasons.append("완성봉 종가가 VWAP 이하")
    # Saturate large moves rather than rewarding volatility without limit.
    target = float(cfg["position_target_return"])
    volatility = min(atr / target, 1) * max(0, min(1, (3 * target - atr) / target))
    if atr >= 3 * target: reasons.append("목표 대비 과도한 5분봉 변동성")
    components = {
        "volume": round(30 * min(relative / 3, 1), 3),
        "turnover": round(25 * min(max(math.log10(max(turnover / float(cfg["min_session_turnover"]), 1)), 0) / 2, 1), 3),
        "strength": round(25 * min(max(strength / .02, 0), 1), 3),
        "volatility": round(20 * volatility, 3),
    }
    return {"eligible": not reasons, "reasons": reasons, "metrics": metrics,
            "components": components, "score": round(sum(components.values()), 3)}


def screen_candidates(candidates, backend, settings, *, requested_date, clock=now_kst):
    """One index snapshot, sequential stock reads, no account writes or LLM calls."""
    now = clock()
    result = {"version": 1, "asOf": now.isoformat(), "status": "completed", "error": None,
              "rows": [], "eligibleCount": 0, "method": "volume30-turnover25-strength25-volatility20"}
    candidates = candidates[:100]
    deadline = now + timedelta(seconds=180)

    def checked_call(method, *args, **kwargs):
        checkpoint("candidate-market-data")
        if clock() >= deadline:
            raise TimeoutError("후보 시장자료 수집 제한시간 초과")
        value = backend.call(method, *args, **kwargs)
        checkpoint("candidate-market-data")
        if clock() >= deadline:
            raise TimeoutError("후보 시장자료 수집 제한시간 초과")
        return value

    try:
        if requested_date != now.date().isoformat():
            raise ValueError("당일 후보 재평가는 오늘 날짜만 지원합니다.")
        calendar = checked_call("get_kr_calendar", requested_date)
        if calendar["today"]["date"] != requested_date:
            raise ValueError("거래일 자료 날짜 불일치")
        session = calendar["today"]["integrated"]["regularMarket"]
        start, end = stamp(session["startTime"]), stamp(session["endTime"])
        configured_open = datetime.combine(now.date(), datetime.strptime(settings.get("market_open", "09:00"), "%H:%M").time(), now.tzinfo)
        configured_close = datetime.combine(now.date(), datetime.strptime(settings.get("market_close", "15:30"), "%H:%M").time(), now.tzinfo)
        cutoff = min(end, configured_close)
        cutoff -= timedelta(minutes=int(settings.get("buy_cutoff_minutes", 30)))
        holidays = {day.strip() for day in settings.get("holiday_dates", "").split(",")}
        if requested_date in holidays or start.date() != now.date() or end.date() != now.date() or not max(start, configured_open) <= now < cutoff:
            raise ValueError("정규장 신규 매수 가능 시간 밖")
        deadline = min(deadline, cutoff)
        # Every candidate uses the same completed candle boundary and KOSPI baseline.
        market_data = result.setdefault("marketData", {})
        market_rows = session_candles(checked_call, "KOSPI", start, now, index=True, diagnostics=market_data)
        reference = market_reference(market_rows, start, now)
        result["marketReference"] = reference
        result["warnings"] = [reference["warning"]] if reference["warning"] else []
        market_return = reference["return"]
        if not math.isfinite(market_return) or market_return <= -float(settings.get("market_drop_limit", ".01")):
            raise ValueError("KOSPI 하락 제한 또는 유효하지 않은 지수")
        result["marketReturn"] = market_return
    except RunStopped:
        raise
    except Exception as exc:
        result.update(status="failed", error=f"시장 기준자료 확인 실패 ({type(exc).__name__}): {exc}")
        if getattr(exc, "diagnostics", None):
            result["dataDiagnostics"] = exc.diagnostics
        result["rows"] = [{**row, "intraday": {"eligible": False, "score": None, "reasons": [result["error"]]}} for row in candidates]
        return result

    blocked = None
    for rank, row in enumerate(candidates, 1):
        checkpoint("candidate-screen")
        data_quality = {}
        try:
            if blocked:
                raise ValueError(blocked)
            symbol = row["ticker"].split(".")[0]
            rows = session_candles(checked_call, symbol, start, now, diagnostics=data_quality)
            check = measure_candidate(rows, start, now, market_return, settings,
                                      reference_start=stamp(reference["basisStart"]))
        except RunStopped:
            raise
        except Exception as exc:
            check = {"eligible": False, "score": None, "reasons": [f"시장자료 제외 ({type(exc).__name__}): {exc}"]}
            result["status"] = "partial"
            # Stop additional requests after throttling, auth failures or budget expiry.
            code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            if isinstance(exc, TimeoutError) or code in {401, 403, 429}:
                blocked = "조회 제한/인증/시간 초과로 추가 수집 중단"
        check["dataQuality"] = data_quality
        result["rows"].append({**row, "newsRank": rank, "intraday": check})
    if clock() >= deadline:
        result.update(status="failed", error="시장자료 수집 제한시간 또는 매수 가능 시간 만료")
        for row in result["rows"]:
            row["intraday"].update(eligible=False, reasons=[result["error"]])
    result["rows"].sort(key=lambda row: (not row["intraday"]["eligible"],
        -(row["intraday"]["score"] or 0), row["newsRank"]))
    result["eligibleCount"] = sum(row["intraday"]["eligible"] for row in result["rows"])
    return result

"""Session-relative entry windows shared by scheduling and final order checks."""

from datetime import timedelta


TIME_DEFAULTS = {
    "buy_start_minutes": 60,
    "buy_cutoff_minutes": 60,
    "candidate_scan_minutes": 60,
    "carry_exit_minutes": 60,
    "liquidation_minutes": 15,
}
TIME_LIMITS = {
    "buy_start_minutes": (20, 120),
    "buy_cutoff_minutes": (5, 120),
    "candidate_scan_minutes": (5, 120),
    "carry_exit_minutes": (5, 60),
    "liquidation_minutes": (5, 120),
}


def buy_window(window, settings, *, regular_open=None):
    if not window:
        return None
    cfg = {**TIME_DEFAULTS, **settings}
    start = max(window[0], (regular_open or window[0]) + timedelta(minutes=int(cfg["buy_start_minutes"])))
    end = window[1] - timedelta(minutes=int(cfg["buy_cutoff_minutes"]))
    return start, end


def entry_window_state(now, window):
    """A normal time veto, distinct from unavailable or malformed market data."""
    result = {"allowed": False, "reason": "closed", "notice": "오늘은 신규 매수 가능 시간이 아닙니다.",
              "startsAt": None, "cutoffAt": None}
    if not window:
        return result
    start, cutoff = window
    if start.date() != now.date() or cutoff.date() != now.date():
        raise ValueError("매수 가능 시간의 거래일이 일치하지 않습니다.")
    result.update(startsAt=start.isoformat(), cutoffAt=cutoff.isoformat())
    if start >= cutoff:
        return result
    if now < start:
        result.update(reason="before-entry", notice=f"신규 매수는 {start:%H:%M}부터 가능합니다. 매수 종료 {cutoff:%H:%M} 전까지 실행하세요.")
    elif now >= cutoff:
        result.update(reason="after-entry", notice=f"오늘 신규 매수는 {cutoff:%H:%M}에 종료되었습니다. 보유 종목 점검·청산은 별도로 계속됩니다.")
    else:
        result.update(allowed=True, reason="open", notice=None)
    return result


def scan_start(now, first, settings):
    interval = timedelta(minutes=int(settings.get("candidate_scan_minutes", TIME_DEFAULTS["candidate_scan_minutes"])))
    return first + max(0, (now - first) // interval) * interval

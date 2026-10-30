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


def scan_start(now, first, settings):
    interval = timedelta(minutes=int(settings.get("candidate_scan_minutes", TIME_DEFAULTS["candidate_scan_minutes"])))
    return first + max(0, (now - first) // interval) * interval

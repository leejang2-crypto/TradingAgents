"""Bind legacy tool signatures to the current run without breaking direct callers."""

from datetime import date

from tradingagents.dataflows.config import get_config


def instrument(requested):
    return get_config().get("analysis_ticker") or requested


def as_of(requested):
    bound = get_config().get("analysis_date")
    if not requested and not bound:
        return None
    value = date.fromisoformat(requested or bound)
    return min(value, date.fromisoformat(bound)).isoformat() if bound else value.isoformat()


def window(start, end):
    # Reject a malformed/inverted window before a vendor is called.
    start = date.fromisoformat(start).isoformat()
    end = as_of(end)
    if start > end:
        raise ValueError("Start date must not be after the analysis window end; correct the dates.")
    return start, end

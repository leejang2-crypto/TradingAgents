"""Deterministic guardrails for local/hybrid LLM trading outputs."""

from __future__ import annotations

import re
from dataclasses import dataclass

from tradingagents.agents.utils.rating import parse_rating


_ACTION_RE = re.compile(r"\b(BUY|SELL|HOLD)\b", re.IGNORECASE)
_STOP_RE = re.compile(
    r"stop\W*loss\W*[:\-]\W*([0-9][0-9,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
_KRW_RE = re.compile(r"([0-9][0-9,]*(?:\.\d+)?)\s*원")
_KOREA_CODE_RE = re.compile(r"\b(\d{6})(?:\.KS)?\b", re.IGNORECASE)


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    reasons: tuple[str, ...] = ()


def _to_float(raw: str) -> float | None:
    try:
        return float(raw.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _extract_current_price(text: str) -> float | None:
    # Prefer lines that explicitly identify the close/current price.
    for line in text.splitlines():
        lower = line.lower()
        if any(label in lower for label in ("종가", "현재", "close", "latest")):
            match = _KRW_RE.search(line)
            if match:
                return _to_float(match.group(1))
    # Fall back to the first KRW-denominated number in the market report.
    match = _KRW_RE.search(text)
    return _to_float(match.group(1)) if match else None


def _extract_action(text: str) -> str | None:
    for line in text.splitlines():
        if "action" in line.lower() or "final transaction proposal" in line.lower():
            match = _ACTION_RE.search(line)
            if match:
                return match.group(1).upper()
    match = _ACTION_RE.search(text)
    return match.group(1).upper() if match else None


def _rating_to_action(rating: str) -> str:
    rating = rating.lower()
    if rating in {"buy", "overweight"}:
        return "BUY"
    if rating in {"sell", "underweight"}:
        return "SELL"
    return "HOLD"


def _looks_truncated(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 80:
        return True
    if stripped.endswith(("**", "*", ":", "-", "매", "매수", "매도")):
        return True
    return stripped.count("**") % 2 == 1


def validate_trading_state(final_state: dict, ticker: str) -> ValidationResult:
    """Return deterministic validation results for local/hybrid agent output.

    This is intentionally conservative. It does not judge investment quality;
    it catches mechanical failures observed in local LLM runs: ticker drift,
    nonsensical price levels, truncated final decisions, and conflicting final
    action chains.
    """
    reasons: list[str] = []
    base_code = ticker.split(".", 1)[0].upper()
    all_text = "\n\n".join(
        str(final_state.get(key, ""))
        for key in (
            "market_report",
            "sentiment_report",
            "news_report",
            "fundamentals_report",
            "investment_plan",
            "trader_investment_plan",
            "final_trade_decision",
        )
    )

    if base_code.isdigit() and len(base_code) == 6:
        code_context_lines = []
        for line in all_text.splitlines():
            lower = line.lower()
            if any(
                marker in lower
                for marker in ("ticker", "symbol", "종목", "대상", "get_stock_data", "get_indicators")
            ):
                code_context_lines.append(line)
        code_context = "\n".join(code_context_lines)
        unexpected_codes = sorted(
            code for code in set(_KOREA_CODE_RE.findall(code_context)) if code != base_code
        )
        if unexpected_codes:
            reasons.append(
                f"unexpected Korean ticker code(s) in output: {', '.join(unexpected_codes)}"
            )

    market_report = str(final_state.get("market_report", ""))
    current_price = _extract_current_price(market_report)
    if current_price:
        for field in ("trader_investment_plan", "final_trade_decision"):
            text = str(final_state.get(field, ""))
            for match in _STOP_RE.finditer(text):
                stop = _to_float(match.group(1))
                if stop and (stop > current_price * 1.75 or stop < current_price * 0.25):
                    reasons.append(
                        f"{field} stop loss {stop:g} is implausible vs current price {current_price:g}"
                    )

    final_decision = str(final_state.get("final_trade_decision", ""))
    if _looks_truncated(final_decision):
        reasons.append("final_trade_decision appears truncated or incomplete")

    trader_action = _extract_action(str(final_state.get("trader_investment_plan", "")))
    portfolio_rating = parse_rating(final_decision, default="Hold")
    portfolio_action = _rating_to_action(portfolio_rating)
    if trader_action in {"BUY", "SELL"} and portfolio_action in {"BUY", "SELL"}:
        if trader_action != portfolio_action:
            reasons.append(
                f"trader action {trader_action} conflicts with portfolio rating {portfolio_rating}"
            )

    return ValidationResult(ok=not reasons, reasons=tuple(reasons))


def hold_decision(reasons: tuple[str, ...]) -> str:
    reason_lines = "\n".join(f"- {reason}" for reason in reasons)
    return (
        "**Rating**: Hold\n\n"
        "**Executive Summary**: Local/hybrid output validation failed, so the "
        "system forced a defensive HOLD instead of allowing an automated trade.\n\n"
        "**Validation Findings**:\n"
        f"{reason_lines}\n\n"
        "**Investment Thesis**: The analysis chain produced mechanically unsafe "
        "signals. Re-run with OpenAI-only or review the local agent outputs before "
        "placing even a dry-run trade.\n\n"
        "**Price Target**: null\n\n"
        "**Time Horizon**: Review required"
    )

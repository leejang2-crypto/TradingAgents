"""Read-only candidate path study. Hypothetical windows are never fill rewards.

Signals use completed 5m prefixes and settings known at that time. Entry is the
next available whole-minute open, not a retrospectively selected intrabar low.
Historical quotes, news, liquidity and account eligibility are not reconstructed.
"""

from collections import Counter
from datetime import date, datetime, timedelta
from decimal import Decimal
import hashlib
import json
import re
import time

import pandas as pd

from tradingagents.brokers.intraday import (
    INTRADAY_DEFAULTS, ENTRY_STRATEGIES, bars, entry_pattern_reasons,
    market_reference, relative_volume, session_candles, stamp,
)
from tradingagents.brokers.paper_account import number
from tradingagents.brokers.toss_transport import read_budget
from tradingagents.brokers.trading_hours import scan_start
from tradingagents.monitoring.control import RunStopped

VERSION = "candidate-path-v1"
SCOPE = "chart-only-counterfactual"
# Missing fields belong to old reports; never apply today's policy retroactively.
TIME_DEFAULTS = {"market_open": "09:00", "market_close": "15:30",
                 "buy_cutoff_minutes": 30, "liquidation_minutes": 15,
                 "buy_start_minutes": 0, "candidate_scan_minutes": 5, "carry_exit_minutes": 60}
OUTCOMES = {"target": "목표 선도달", "stop": "손절 선도달", "ambiguous": "동일 분봉 순서 불명",
            "liquidation": "청산 시각 종료", "stagnation": "정체 종료", "incomplete": "후속 자료 부족"}


def rule_key(settings):
    rules = {key: str(settings.get(key, value)) for key, value in {**INTRADAY_DEFAULTS, **TIME_DEFAULTS}.items()}
    return hashlib.sha256(json.dumps(rules, sort_keys=True).encode()).hexdigest()


def candidates(inputs, day):
    """Availability is an observed timestamp, never inferred from a later price."""
    result = {}

    def add(ticker, name, at, settings, replay, reasons):
        if not isinstance(ticker, str) or not re.fullmatch(r"[0-9A-Z]{6}(?:\.K[QS])?", ticker):
            return
        symbol = ticker.split(".")[0]
        item = result.setdefault(symbol, {"symbol": symbol, "ticker": ticker, "name": name or symbol,
                                           "observations": [], "saved": []})
        try:
            when = stamp(at)
            if when.date() != day:
                return
        except (ValueError, TypeError):
            return
        if isinstance(settings, dict) and settings:
            item["observations"].append({"at": when.isoformat(), "settings": settings,
                                          "reasons": reasons or []})
        if isinstance(replay, dict) and replay.get("candles"):
            item["saved"].append(replay)

    for snapshot in inputs.get("candidates", []):
        screen = snapshot.get("intradayScreen") or {}
        for row in snapshot.get("candidates", []):
            intraday = row.get("intraday") or {}
            replay = intraday.get("replay") or {}
            add(row.get("ticker"), row.get("name"), replay.get("asOf") or screen.get("asOf"),
                replay.get("settings") or screen.get("settings"), replay, intraday.get("reasons"))
    for replay in inputs.get("replays", []):
        technical = replay.get("technical") or {}
        add(technical.get("ticker"), None, technical.get("asOf"), technical.get("settings"),
            technical, (technical.get("signal") or {}).get("reasons"))
    for item in result.values():
        item["observations"].sort(key=lambda row: row["at"])
    return result


def session(inputs, day, item=None):
    regular = ((inputs.get("market", {}).get("calendar", {}).get("today", {}).get("integrated") or {})
               .get("regularMarket"))
    if regular:
        start, end = stamp(regular["startTime"]), stamp(regular["endTime"])
    elif item and item["saved"]:
        start = stamp(item["saved"][0]["start"])
        cfg = item["observations"][0]["settings"]
        end = datetime.combine(day, datetime.strptime(cfg.get("market_close", "15:30"), "%H:%M").time(), start.tzinfo)
    else:
        raise ValueError("session-times-unavailable")
    if start.date() != day or end.date() != day or end <= start:
        raise ValueError("invalid-session-times")
    return start, end


def collect_prices(inputs, day, backend=None, cached=None):
    """Sequential bounded GETs; a shared throttle/auth/budget failure stops fan-out."""
    found = candidates(inputs, day)
    result = {}
    deadline = time.monotonic() + 120
    blocked = None
    for index, (symbol, item) in enumerate(sorted(found.items())):
        previous = (cached or {}).get(symbol, {})
        if previous.get("status") == "complete":
            result[symbol] = previous
            continue
        if not item["observations"]:
            result[symbol] = {"status": "unavailable", "reason": "candidate-time-or-settings-missing"}
            continue
        if backend is None or inputs.get("market", {}).get("status") != "complete":
            result[symbol] = {"status": "unavailable", "reason": "closing-fetch-unavailable"}
            continue
        if blocked or index >= 250 or time.monotonic() >= deadline:
            result[symbol] = {"status": "deferred", "reason": blocked or "collection-budget"}
            continue
        try:
            start, end = session(inputs, day)
            if backend.clock() < end + timedelta(minutes=5):
                raise ValueError("session-not-closed")
            with read_budget(min(20, deadline - time.monotonic())):
                rows = session_candles(backend.call, symbol, start, end,
                                       cache_namespace=getattr(backend, "market_cache_namespace", None))
            result[symbol] = {"status": "complete", "candles": rows}
        except RunStopped:
            raise
        except Exception as exc:
            code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
            result[symbol] = {"status": "unavailable", "reason": type(exc).__name__,
                              "diagnostics": getattr(exc, "diagnostics", None)}
            if isinstance(exc, TimeoutError) or code in {401, 403, 429}:
                blocked = "rate-limit" if code == 429 else "auth-or-timeout"
    return result


def observed_rows(item, closing):
    if closing.get("status") == "complete":
        return closing["candles"], "closing-fetch"
    # Snapshots may overlap, but contradicting data must not be silently overwritten.
    rows = {}
    for saved in item["saved"]:
        as_of = stamp(saved["asOf"])
        for row in saved["candles"]:
            at = stamp(row["timestamp"])
            if at > as_of:
                raise ValueError("future-candle-in-snapshot")
            if at in rows and rows[at] != row:
                raise ValueError("conflicting-snapshot-candle")
            rows[at] = row
    return list(rows.values()), "saved-prefix"


def path_outcome(rows, entry_at, entry, stop, target, until, cost, stagnation_minutes):
    """End-stamped 1m intervals cannot establish the ordering of both barriers."""
    high, low = entry, entry
    for row in rows:
        end = stamp(row["timestamp"])
        if end <= entry_at or end > until:
            continue
        opening, h, l, close = [number(row[key]) for key in ("openPrice", "highPrice", "lowPrice", "closePrice")]
        high, low = max(high, h), min(low, l)
        outcome, exit_price = None, None
        if opening <= stop:
            outcome, exit_price = "stop", opening
        elif opening >= target:
            outcome, exit_price = "target", target
        elif h >= target and l <= stop:
            outcome = "ambiguous"
        elif l <= stop:
            outcome, exit_price = "stop", stop
        elif h >= target:
            outcome, exit_price = "target", target
        elif end >= entry_at + timedelta(minutes=stagnation_minutes) and close < entry * Decimal("1.002"):
            outcome, exit_price = "stagnation", close
        elif end == until:
            outcome, exit_price = "liquidation", close
        if outcome:
            return {"outcome": outcome, "exitIntervalStart": (end - timedelta(minutes=1)).isoformat(),
                    "exitAt": end.isoformat(), "exitPrice": str(exit_price) if exit_price is not None else None,
                    "grossReturn": str(exit_price / entry - 1) if exit_price is not None else None,
                    "estimatedNetReturn": str(exit_price / entry - 1 - cost) if exit_price is not None else None,
                    "mfe": str(high / entry - 1), "mae": str(low / entry - 1)}
    return {"outcome": "incomplete", "exitAt": None, "grossReturn": None, "estimatedNetReturn": None,
            "mfe": str(high / entry - 1), "mae": str(low / entry - 1)}


def hindsight_range(rows, first_seen, end, settings, regular_start=None):
    """Describe available price movement even when no entry rule matched.

    This deliberately uses future highs and ignores stops. It is an upper-bound
    diagnostic, not a signal, return estimate, or missed executable profit.
    """
    cfg = {**INTRADAY_DEFAULTS, **TIME_DEFAULTS, **settings}
    close = min(end, end.replace(hour=int(cfg["market_close"][:2]), minute=int(cfg["market_close"][3:])))
    cutoff = close - timedelta(minutes=int(cfg["buy_cutoff_minutes"]))
    until = close - timedelta(minutes=int(cfg["liquidation_minutes"]))
    opening_time = end.replace(hour=int(cfg["market_open"][:2]), minute=int(cfg["market_open"][3:]))
    opening_time = max(opening_time, (regular_start or opening_time) + timedelta(minutes=int(cfg["buy_start_minutes"])))
    cheapest, best, first_target = None, None, None
    for row in rows:
        at = stamp(row["timestamp"]) - timedelta(minutes=1)
        if at < max(first_seen, opening_time) or stamp(row["timestamp"]) > until:
            continue
        opening, high = number(row["openPrice"]), number(row["highPrice"])
        if at < cutoff and (cheapest is None or opening < cheapest[1]):
            cheapest = at, opening
        if cheapest is None:
            continue
        rise = high / cheapest[1] - 1
        current = {"entryAt": cheapest[0].isoformat(), "entry": str(cheapest[1]),
                   "highIntervalStart": at.isoformat(), "highAt": row["timestamp"],
                   "high": str(high), "grossRise": str(rise)}
        if best is None or rise > number(best["grossRise"]):
            best = current
        if first_target is None and rise >= number(cfg["position_target_return"]):
            first_target = current
    return {"scope": "hindsight-no-stop-upper-bound", "best": best, "firstTarget": first_target,
            "targetReturn": str(cfg["position_target_return"]), "settingsAt": first_seen.isoformat()}


def recorded_decisions(inputs, symbol, day):
    found = {}
    for summary in inputs.get("summaries", []):
        for row in summary.get("analyses", []):
            if str(row.get("ticker", "")).split(".")[0] != symbol:
                continue
            contract = row.get("intradayContract") or {}
            try:
                at = stamp(contract["evaluatedAt"])
                if at.date() != day:
                    continue
            except (ValueError, TypeError, KeyError):
                continue
            found[at] = {"at": at.isoformat(), "action": contract.get("action"), "reasons": contract.get("reasons", [])}
    return [found[key] for key in sorted(found)]


def analyze_candidate(item, inputs, day):
    record = {key: item[key] for key in ("symbol", "ticker", "name")}
    record.update(status="unavailable", windows=[], blockReasons=[], scope=SCOPE, executionVerified=False)
    if not item["observations"]:
        return {**record, "reason": "후보 포착 시각 또는 당시 설정 미기록"}
    try:
        start, end = session(inputs, day, item)
        closing = inputs.get("candidatePrices", {}).get(item["symbol"], {})
        rows, source = observed_rows(item, closing)
        rows = sorted(rows, key=lambda row: stamp(row["timestamp"]))
        if not rows or any(not start < stamp(row["timestamp"]) <= end for row in rows):
            raise ValueError("missing-or-wrong-session-candles")
        through = stamp(rows[-1]["timestamp"])
        through = start + timedelta(minutes=int((through - start).total_seconds() // 300) * 5)
        bars(rows, start, through)  # Validate contiguous OHLCV before any hypothetical result.
        rows = [row for row in rows if stamp(row["timestamp"]) <= through]
        first_seen = stamp(item["observations"][0]["at"])
        complete = source == "closing-fetch" and through == end
        record.update(status="complete" if complete else "partial", firstSeen=first_seen.isoformat(),
                      evaluatedThrough=through.isoformat(), source=source, collectionIssue=closing.get("reason"))
        record["priceRange"] = hindsight_range(rows, first_seen, end, item["observations"][0]["settings"], start)
        decisions = recorded_decisions(inputs, item["symbol"], day)
        record["recordedDecisions"] = decisions
        timeline = {max(first_seen, start + timedelta(minutes=20))}
        timeline.update(start + timedelta(minutes=i) for i in range(20, int((through - start).total_seconds() // 60) + 1, 5)
                        if start + timedelta(minutes=i) >= first_seen)
        available = {stamp(row["timestamp"]) - timedelta(minutes=1): row for row in rows}
        reasons = Counter()
        busy_until = {strategy: start for strategy in ENTRY_STRATEGIES}
        for at in sorted(timeline):
            if at >= through:
                continue
            observations = [row for row in item["observations"] if stamp(row["at"]) <= at]
            if not observations:
                continue
            observation = observations[-1]
            cfg = {**INTRADAY_DEFAULTS, **TIME_DEFAULTS, **observation["settings"]}
            configured_end = datetime.combine(day, datetime.strptime(cfg["market_close"], "%H:%M").time(), start.tzinfo)
            configured_start = datetime.combine(day, datetime.strptime(cfg["market_open"], "%H:%M").time(), start.tzinfo)
            until = min(end, configured_end) - timedelta(minutes=int(cfg["liquidation_minutes"]))
            cutoff = min(end, configured_end) - timedelta(minutes=int(cfg["buy_cutoff_minutes"]))
            entry_start = max(configured_start, start + timedelta(minutes=int(cfg["buy_start_minutes"])))
            initial_check = max(first_seen, entry_start, start + timedelta(minutes=20))
            if at != initial_check and at != scan_start(at, entry_start, cfg):
                continue
            # A 09:20:05 observation cannot buy at the already passed 09:20 open.
            entry_at = at.replace(second=0, microsecond=0)
            if entry_at < at:
                entry_at += timedelta(minutes=1)
            if not entry_start <= entry_at < min(cutoff, until) or entry_at not in available:
                continue
            prefix = [row for row in rows if stamp(row["timestamp"]) <= at]
            five, turnover, vwap = bars(prefix, start, at)
            relative = relative_volume(five)
            last, prior = five.iloc[-1], five.iloc[-2]
            if turnover < float(number(cfg["min_session_turnover"])) or relative < float(number(cfg["min_relative_volume"])) or last.close <= vwap:
                reasons["거래대금/상대거래량/VWAP 조건 미충족"] += 1
                continue
            market_state = "unverified"
            indices = inputs.get("market", {}).get("indices", {})
            if all(key in indices for key in ("KOSPI", "KOSDAQ")):
                returns = [market_reference(indices[key]["candles"], start, at)["return"] for key in ("KOSPI", "KOSDAQ")]
                if min(returns) <= -float(number(cfg["market_drop_limit"])):
                    reasons["당시 지수 하락 제한"] += 1
                    continue
                market_state = "passed"
            entry = number(available[entry_at]["openPrice"])
            if abs(entry / number(str(last.close)) - 1) > number(cfg["max_entry_extension"]):
                reasons["다음 분봉 시가가 추격 제한 초과"] += 1
                continue
            previous = five.close.shift(1)
            atr = number(str(pd.concat([five.high - five.low, (five.high - previous).abs(),
                                       (five.low - previous).abs()], axis=1).max(axis=1).tail(14).mean()))
            stop = max(entry * (1 - number(cfg["position_stop_loss"])), entry - atr, number(str(prior.low)))
            target_rate, cost = number(cfg["position_target_return"]), number(cfg["estimated_cost_rate"])
            distance = (entry - stop) / entry
            if stop >= entry or distance + cost <= 0 or (target_rate - cost) / (distance + cost) < number(cfg["min_reward_risk"]):
                reasons["손절선/비용 반영 손익비 부족"] += 1
                continue
            for strategy in ENTRY_STRATEGIES:
                if entry_at < busy_until[strategy]:
                    continue
                variant = {**cfg, "entry_strategy": strategy}
                blocked = entry_pattern_reasons(five, prefix, start, vwap, variant)
                if blocked:
                    reasons.update(blocked)
                    continue
                target = entry * (1 + target_rate)
                outcome = path_outcome(rows, entry_at, entry, stop, target, until, cost, int(cfg["stagnation_minutes"]))
                busy_until[strategy] = stamp(outcome["exitAt"]) if outcome["exitAt"] else through
                record["windows"].append({"strategy": strategy, "configuredStrategy": strategy == cfg["entry_strategy"],
                    "ruleKey": rule_key(variant), "signalAt": at.isoformat(), "barEnd": five.index[-1].isoformat(),
                    "entryAt": entry_at.isoformat(), "entry": str(entry), "stop": str(stop), "target": str(target),
                    "targetReturn": str(target_rate), "estimatedCostRate": str(cost),
                    "vwap": vwap, "relativeVolume": relative, "marketCheck": market_state,
                    "observedReasons": observation["reasons"],
                    "recordedDecision": next((row for row in reversed(decisions) if stamp(row["at"]) <= at), None),
                    **outcome})
        record["blockReasons"] = reasons.most_common(8)
        record["targetWindows"] = sum(row["outcome"] == "target" for row in record["windows"])
        record["ambiguousWindows"] = sum(row["outcome"] == "ambiguous" for row in record["windows"])
        return record
    except (ValueError, TypeError, KeyError, IndexError, ArithmeticError) as exc:
        return {**record, "status": "unavailable", "windows": [], "reason": f"분봉/설정 검증 실패: {exc}"}


def analyze(inputs, day):
    rows = [analyze_candidate(item, inputs, day) for _, item in sorted(candidates(inputs, day).items())]
    return {"version": VERSION, "scope": SCOPE, "date": day.isoformat(), "executionVerified": False,
            "status": "complete" if all(row["status"] == "complete" for row in rows) else "partial",
            "candidateCount": len(rows), "completeCount": sum(row["status"] == "complete" for row in rows),
            "priceTargetCandidateCount": sum(bool(row.get("priceRange", {}).get("firstTarget")) for row in rows),
            "targetCandidateCount": sum(bool(row.get("targetWindows")) for row in rows),
            "candidates": rows}


def compact(result):
    # Full evidence remains in the external archive. The local cache is bounded.
    return {**result, "candidates": [{**row, "windows": row["windows"][:12],
                                      "recordedDecisions": row.get("recordedDecisions", [])[-3:],
                                      "windowCount": len(row["windows"]), "windowsTruncated": len(row["windows"]) > 12}
                                     for row in result["candidates"]]}


def prior_reference(local_dir, mode, account, ticker, now, settings):
    from .postmarket import read_json, state_path
    try:
        record = read_json(state_path(local_dir, mode, account), {})
        result = record.get("candidateReview", {})
        source = date.fromisoformat(result["date"])
        if (record.get("mode") != mode or record.get("account") != account or result.get("version") != VERSION
                or result.get("scope") != SCOPE or not source < now.date() <= source + timedelta(days=7)):
            return None
        candidate = next((row for row in result["candidates"] if row["symbol"] == ticker.split(".")[0]
                          and row["status"] == "complete"), None)
        if candidate is None:
            return None
        windows = [row for row in candidate["windows"] if row.get("ruleKey") == rule_key(settings)]
        for row in windows:
            if (row["strategy"] not in ENTRY_STRATEGIES or row["outcome"] not in OUTCOMES
                    or stamp(row["entryAt"]).date() != source or stamp(row["signalAt"]) > stamp(row["entryAt"])
                    or (row.get("exitAt") and (stamp(row["exitAt"]).date() != source
                                               or stamp(row["exitAt"]) <= stamp(row["entryAt"])))
                    or not 0 < number(row["stop"]) < number(row["entry"]) < number(row["target"])):
                return None
        return {"scope": SCOPE, "sourceDate": source.isoformat(), "ticker": ticker,
                "referenceOnly": True, "matchingWindows": windows[:3],
                "windowsTruncated": candidate.get("windowsTruncated", False),
                "blockReasons": candidate["blockReasons"], "reportPath": record.get("reportPath"),
                "note": "과거 차트 가정 분석. 실제 체결 성과 아님. 오늘 분봉·호가·뉴스·계좌 조건을 모두 재검사."}
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def markdown(result):
    lines = ["## 후보 가격흐름 복기 (가상 관찰)",
             f"- 후보 {result['candidateCount']}개 / 종가까지 자료 확인 {result['completeCount']}개 / 목표 선도달 구간 관찰 {result['targetCandidateCount']}개",
             "- 후보 포착 이후 완성 5분봉으로 신호를 확인하고, 그 이후 사용 가능한 1분봉 시가를 진입 가격으로 가정합니다.",
             "- 4가지 패턴의 비교 시나리오입니다. 패턴별 구간은 겹치지 않지만 패턴 간 중복/청산 후 재진입 가정이 있어 실매매 횟수·승률·일일 수익률로 합산하지 않습니다.",
             "- 호가·뉴스·계좌·체결 가능성은 확인하지 못합니다. 다른 패턴은 연구용이며 현재 매매 설정을 바꾸지 않습니다.",
             "- 목표는 세전 가격 상승률이며 비용 차감 추정은 별도입니다. 동일 1분봉에서 목표와 손절을 모두 통과하면 순서를 확정하지 않습니다.",
             "- MFE/MAE는 관찰 구간 최고/최저 변동이며 봉 내부의 발생 순서를 보장하지 않습니다.\n"]
    for row in result["candidates"]:
        name = str(row["name"]).replace("\n", " ").replace("|", " ")
        lines.extend([f"### {name} ({row['symbol']})", f"- 자료: {row['status']} / 최초 포착: {row.get('firstSeen', '미기록')} / 관측 종료: {row.get('evaluatedThrough', '미확인')}"])
        price_range = row.get("priceRange") or {}
        best = price_range.get("best")
        if best:
            lines.append(f"- 사후 가격 상승폭 상한: {best['entryAt'][11:19]} 시가 {best['entry']} → {best['highAt'][11:19]} 종료 분봉 고가 {best['high']}, {number(best['grossRise']):+.3%}. 손절·비용·진입 신호 무시, 실현 가능한 수익 아님.")
        if price_range.get("firstTarget") and not row["windows"]:
            lines.append("- 가격상 목표 구간은 있었지만 검증된 차트 가정 진입은 없습니다. 아래 차단 사유/자료 누락을 점검하세요.")
        for decision in row.get("recordedDecisions", []):
            lines.append(f"- 실제 기록된 분석: {decision['at'][11:19]} {decision['action']} / {'; '.join(decision['reasons'])} (체결 여부와 별개)")
        if row.get("reason"):
            lines.append(f"- {row['reason']}")
        lines.extend(f"- 차트 차단: {reason} ({count}회)" for reason, count in row["blockReasons"])
        if not row["windows"]:
            lines.append("- 확인 가능한 가상 진입 구간 없음. 자료 부족과 조건 미충족은 구분하며, 수익 기회가 전혀 없었다는 뜻은 아닙니다.\n")
            continue
        lines.extend(["| 패턴 | 신호 / 가정 진입 | 진입 / 목표 / 손절 | 결과 / 도달 분봉 구간 | 비용 차감 추정 |",
                      "|---|---|---|---|---:|"])
        for window in row["windows"]:
            net = window.get("estimatedNetReturn")
            net_text = f"{number(net):+.3%}" if net is not None else "미확정"
            label = ENTRY_STRATEGIES[window["strategy"]] + (" (당시 설정)" if window["configuredStrategy"] else " (비교)")
            lines.append(f"| {label} | {window['signalAt'][11:19]} / {window['entryAt'][11:19]} | {window['entry']} / {window['target']} / {window['stop']} | {OUTCOMES[window['outcome']]} / {str(window.get('exitIntervalStart') or '')[11:19]}~{str(window.get('exitAt') or '')[11:19]} | {net_text} |")
        lines.append("")
    return "\n".join(lines)


def reference_markdown(reference):
    lines = ["## 이전 후보 가격흐름 참고 (체결 성과 아님)",
             f"- 기준일 {reference['sourceDate']}. {reference.get('note', '')}"]
    for row in reference.get("matchingWindows", []):
        lines.append(f"- {ENTRY_STRATEGIES[row['strategy']]}: 가정 진입 {row['entryAt'][11:19]}, "
                     f"{OUTCOMES[row['outcome']]} {str(row.get('exitAt') or '')[11:19]}. "
                     f"진입 {row['entry']} / 목표 {row['target']} / 손절 {row['stop']}원.")
        decision = row.get("recordedDecision")
        if decision:
            lines.append(f"  당시 선행 판정 {decision['action']}: {'; '.join(decision['reasons'])}")
    if not reference.get("matchingWindows"):
        lines.append("- 현재 규칙과 일치하는 가상 진입 구간은 저장된 참고 표본에 없습니다.")
    lines.extend(f"- 당시 차트 차단: {reason} ({count}회)" for reason, count in reference.get("blockReasons", []))
    if reference.get("windowsTruncated"):
        lines.append("- 일부 표본만 표시합니다. 전체 구간은 외장 복기 보고서에 있습니다.")
    lines.append("- 오늘 판정/진입 조건은 변경하지 않습니다. 오늘 시장자료가 우선입니다.")
    return "\n".join(lines) + "\n"

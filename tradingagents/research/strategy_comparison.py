"""Read-only, paired intraday experiments. No orders, LLMs or policy promotion.

Only recorded orderbooks can authorize a technical signal. Subsequent candle
paths are labelled estimates, never fills or account-level returns.
"""

from collections import Counter
from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
import re
import sqlite3
import threading
import pandas as pd

from tradingagents.brokers.intraday import (INTRADAY_DEFAULTS, bars, evaluate_entry,
                                           entry_pattern_reasons, stamp)
from tradingagents.brokers.intraday_analysis import collect_technical
from tradingagents.brokers.paper_account import KST, number
from tradingagents.brokers.trading_hours import TIME_DEFAULTS, buy_window
from . import postmarket as daily, candidate_review, weekly_review

VERSION = "intraday-comparison-v1"
DEFAULTS = {"strategy_observation": False}
ARCHIVE_ROOT = daily.VOLUME / "TradingAgents/strategy_research"
EXPERIMENTS = ("baseline", "time_volume", "breakout_retest")
LIMITATIONS = [
    "주문 없는 기술 신호 비교입니다. 뉴스·계좌 심사와 실제 체결을 재현한 백테스트가 아닙니다.",
    "신호별 다음 분봉 시가와 이후 차트 경로는 가정입니다. 겹치는 신호의 수익을 계좌 수익률로 합산하지 않습니다.",
    "과거 동일 시간대 10일 미만·누락·큰 가격 단절은 시간대 거래량 미평가입니다. 권리락 원인 자체를 판별하지 않습니다.",
    "기존 시간 단위 분석에는 후보 관찰 시작 기록이 없어 재확인 전략을 소급 평가하지 않습니다. 새 5분 관찰 자료가 필요합니다.",
    "자료가 있는 후보만 비교하며 전체 코스피200 성과를 의미하지 않습니다. 수익 보장·자동 전략 적용은 없습니다.",
]


def archive_available():
    if not daily.VOLUME.is_mount() or not ARCHIVE_ROOT.resolve().is_relative_to(daily.VOLUME.resolve()):
        raise OSError("External 연구 저장소를 사용할 수 없습니다.")
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)


def cost_profile(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError("비용 기준 형식이 올바르지 않습니다.")
    result = {}
    for key, default in {"feeRate": "0", "sellTaxRate": "0", "slippageRate": "0", "roundTripFloor": ".003"}.items():
        rate = number(value.get(key, default))
        if not 0 <= rate <= Decimal(".05"):
            raise ValueError("비용 비율은 0~0.05 범위여야 합니다.")
        result[key] = str(rate)
    # Slippage is per side; the floor is inclusive, not an additional charge.
    total = 2 * number(result["feeRate"]) + number(result["sellTaxRate"]) + 2 * number(result["slippageRate"])
    if total > Decimal(".05"):
        raise ValueError("왕복 비용 합계는 5% 이하여야 합니다.")
    result["effectiveRoundTrip"] = str(max(total, number(result["roundTripFloor"])))
    return result


def paper_costs(local_dir):
    path = Path(local_dir) / "accounts/paper.sqlite3"
    if not path.exists():
        return None
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute("SELECT fee_rate,tax_rate,slippage FROM account WHERE id=1").fetchone()
        has_profiles = db.execute("SELECT 1 FROM sqlite_master WHERE name='cost_profiles'").fetchone()
        revision = db.execute("SELECT max(revision) FROM cost_profiles").fetchone()[0] if has_profiles else 0
    return {"revision": revision or 0, "rates": dict(zip(("feeRate", "sellTaxRate", "slippageRate"), row))} if row else None


def settings_key(settings):
    return daily.digest({k: settings.get(k, v) for k, v in {**INTRADAY_DEFAULTS, **TIME_DEFAULTS,
        "market_open": "09:00", "market_close": "15:30", "holiday_dates": "", "paper_capital": "10000000",
        "position_pct": ".10", "candidate_scan_minutes": 60}.items()})


def time_relative_volume(technical, history):
    start, now = stamp(technical["start"]), stamp(technical["asOf"])
    five, _, _ = bars(technical["candles"], start, now)
    offset = five.index[-1].to_pydatetime() - start
    samples, excluded = [], 0
    prior = {row["date"]: row for row in history if row["symbol"] == technical["ticker"].split(".")[0]
             and date.fromisoformat(row["date"]) < now.date()}
    for day in sorted(prior)[-20:]:
        item = prior[day]
        try:
            opening, end = stamp(item["start"]), stamp(item["end"])
            if opening.date().isoformat() != day or end.date() != opening.date() or opening + offset > end:
                raise ValueError("Session mismatch")
            past, _, _ = bars(item["candles"], opening, opening + offset)
            # A large discontinuity cannot safely be treated as comparable volume.
            ratio = float(five.open.iloc[0]) / float(past.open.iloc[0])
            if not .8 <= ratio <= 1.2 or past.index[-1] != opening + offset or past.volume.iloc[-1] <= 0:
                raise ValueError("Non-comparable historical bar")
            samples.append({"date": day, "volume": float(past.volume.iloc[-1])})
        except (ValueError, KeyError, IndexError, TypeError):
            excluded += 1
    average = sum(row["volume"] for row in samples) / len(samples) if samples else None
    return {"status": "ready" if len(samples) >= 10 else "insufficient-history", "samples": len(samples),
            "excluded": excluded, "baseline": average, "ratio": float(five.volume.iloc[-1]) / average
            if len(samples) >= 10 and average else None, "dates": [row["date"] for row in samples]}


def breakout_retest(five, *, scope_start, tick):
    """Freeze the latest fresh breakout's level; no later-bar recalculation."""
    last_index = len(five) - 1
    for i in range(last_index - 1, max(2, last_index - 4), -1):
        if five.index[i] < scope_start:
            continue
        level = float(five.high.iloc[i - 3:i].max())
        if not five.close.iloc[i] > level:
            continue
        previous = five.close.iloc[:i + 1].shift(1)
        tr = pd.concat([five.high.iloc[:i + 1] - five.low.iloc[:i + 1],
                        (five.high.iloc[:i + 1] - previous).abs(),
                        (five.low.iloc[:i + 1] - previous).abs()], axis=1).max(axis=1)
        tolerance = max(float(tick), float(tr.tail(14).mean()) * .1)
        following = five.iloc[i + 1:]
        if (following.close < level).any() or (following.low < level - tolerance).any():
            return {"allowed": False, "reason": "돌파 기준선 이탈", "level": level}
        # Earlier retest consumes the setup even if a later bar also touches it.
        touched = following[(following.low <= level + tolerance) & (following.close > level)
                            & (following.close > following.open)]
        if len(touched) and touched.index[0] == five.index[-1]:
            return {"allowed": True, "level": level, "breakoutAt": five.index[i].isoformat(),
                    "tolerance": tolerance, "stopPrice": str(number(str(five.low.iloc[-1])) - tick)}
        return {"allowed": False, "reason": "돌파 후 신규 지지 재확인 대기", "level": level}
    return {"allowed": False, "reason": "관찰 범위 내 3봉 이내 돌파 없음"}


def evaluate_sample(technical, history, profile, scope_start):
    cfg = {**INTRADAY_DEFAULTS, **TIME_DEFAULTS, **technical["settings"]}
    cfg["estimated_cost_rate"] = str(max(number(cfg["estimated_cost_rate"]), number(profile["effectiveRoundTrip"])))
    start, now = stamp(technical["start"]), stamp(technical["asOf"])
    if now.date() != start.date() or stamp(scope_start) > now:
        raise ValueError("Invalid observation scope")
    end = now.replace(hour=int(cfg.get("market_close", "15:30")[:2]), minute=int(cfg.get("market_close", "15:30")[3:]), second=0, microsecond=0)
    end = min(end, stamp(technical.get("end", end.isoformat())))
    configured_start = now.replace(hour=int(cfg.get("market_open", "09:00")[:2]), minute=int(cfg.get("market_open", "09:00")[3:]), second=0, microsecond=0)
    entry_start, cutoff = buy_window((max(start, configured_start), end), cfg, regular_open=start)
    if not entry_start <= now < cutoff or str(now.date()) in cfg.get("holiday_dates", "").split(","):
        raise ValueError("Observation outside entry window")
    base = evaluate_entry(technical["ticker"].split(".")[0], technical["candles"], technical["orderbook"],
                          technical["markets"], start, now, cfg)
    budget = number(cfg.get("paper_capital", "10000000")) * number(cfg.get("position_pct", ".1"))
    if number(base["ask"]) * (1 + number(cfg["estimated_cost_rate"])) > budget:
        base["reasons"].append("종목당 예산으로 1주 매수 불가")
        base["allowed"] = False
    volume = time_relative_volume(technical, history)
    timed = deepcopy(base)
    if volume["ratio"] is None:
        timed["reasons"].append("동일 시간대 거래량 표본 부족")
    elif volume["ratio"] < float(cfg["min_relative_volume"]):
        timed["reasons"].append("동일 시간대 거래량 부족")
    # An independent experiment: substitute the volume test, preserve all others.
    timed["reasons"] = [r for r in timed["reasons"] if r != "최근 5분봉 대비 거래량 부족"]
    timed["allowed"] = not timed["reasons"]
    five, _, vwap = bars(technical["candles"], start, now)
    prices = sorted({number(r["price"]) for side in ("asks", "bids") for r in technical["orderbook"][side]})
    # Observed spacing is an upper-bound proxy, not an exchange tick-size claim.
    tick = min((b - a for a, b in zip(prices, prices[1:]) if b > a), default=Decimal(1))
    pattern = breakout_retest(five, scope_start=stamp(scope_start), tick=tick)
    retest = deepcopy(base)
    pattern_reasons = entry_pattern_reasons(five, technical["candles"], start, vwap, cfg)
    replaced = set(pattern_reasons + ["손절선이 현재 매수호가 이상", "예상 비용 반영 손익비 부족"])
    retest["reasons"] = [r for r in base["reasons"] if r not in replaced]
    if not pattern["allowed"]:
        retest["reasons"].append(pattern["reason"])
    else:
        ask, bid, stop = number(base["ask"]), number(base["bid"]), number(pattern["stopPrice"])
        distance, cost = (ask - stop) / ask, number(cfg["estimated_cost_rate"])
        ratio = (number(cfg["position_target_return"]) - cost) / (max(Decimal(0), distance) + cost) if distance + cost > 0 else Decimal(0)
        if not 0 < stop < bid or not 0 < distance <= number(cfg["position_stop_loss"]):
            retest["reasons"].append("재확인 손절선과 손실 한도 불일치")
        if ratio < number(cfg["min_reward_risk"]):
            retest["reasons"].append("예상 비용 반영 손익비 부족")
        retest.update(stopPrice=str(stop), stopFraction=str(distance), rewardRisk=str(ratio))
    retest.update(allowed=not retest["reasons"], strategy="breakout_retest", pattern=pattern)
    return {"baseline": base, "time_volume": timed, "breakout_retest": retest}, volume, cfg


def path_estimate(technical, signal, settings, closing):
    if not signal["allowed"]:
        return {"outcome": "blocked"}
    if not closing:
        return {"outcome": "unavailable"}
    now = stamp(technical["asOf"])
    start, end = stamp(closing["start"]), stamp(closing["end"])
    rows = sorted(closing["candles"], key=lambda r: r["timestamp"])
    bars(rows, start, end)  # No silently bridged missing exit intervals.
    future = [r for r in rows if stamp(r["timestamp"]) - timedelta(minutes=1) > now]
    if not future:
        return {"outcome": "unavailable"}
    entry_at, entry = stamp(future[0]["timestamp"]) - timedelta(minutes=1), number(future[0]["openPrice"])
    configured_end = end.replace(hour=int(settings.get("market_close", "15:30")[:2]), minute=int(settings.get("market_close", "15:30")[3:]))
    end = min(end, configured_end)
    _, cutoff = buy_window((start, end), settings, regular_open=start)
    until = end - timedelta(minutes=int(settings["liquidation_minutes"]))
    cost, stop = number(settings["estimated_cost_rate"]), number(signal["stopPrice"])
    distance = (entry - stop) / entry
    if (entry_at >= min(cutoff, until) or entry_at - now > timedelta(minutes=2)
            or abs(entry / number(signal["ask"]) - 1) > number(settings["max_entry_extension"])
            or not 0 < distance <= number(settings["position_stop_loss"])
            or (number(settings["position_target_return"]) - cost) / (distance + cost) < number(settings["min_reward_risk"])):
        return {"outcome": "entry-invalidated"}
    result = candidate_review.path_outcome(rows, entry_at, entry, stop,
        entry * (1 + number(settings["position_target_return"])), until, cost, int(settings["stagnation_minutes"]))
    return {**result, "entryAt": entry_at.isoformat(), "entryPrice": str(entry), "costRate": str(cost),
            "scope": "next-minute-open-chart-estimate-not-a-fill"}


def load_sources(mode, account, now):
    root = daily.ARCHIVE_ROOT / mode / account
    days = sorted(p.name for p in root.glob("????-??-??") if p.name <= str(now.date()))[-60:]
    if not days:
        return [], [], [], []
    records, sources, issues = weekly_review.collect_archives(mode, account, days)
    history, observations = [], []
    for day, record in sorted(records.items()):
        if day not in days:
            continue
        inputs = record["inputs"]
        try:
            start, end = candidate_review.session(inputs, date.fromisoformat(day))
            if now < end + timedelta(minutes=5):
                continue
            for symbol, row in inputs.get("candidatePrices", {}).items():
                if row.get("status") == "complete":
                    bars(row["candles"], start, end)
                    history.append({"symbol": symbol, "date": day, "start": start.isoformat(),
                                    "end": end.isoformat(), "candles": row["candles"]})
            for replay in inputs.get("replays", []):
                technical = replay.get("technical") or {}
                if technical.get("orderbook") and technical.get("asOf", "~") <= now.isoformat():
                    # Legacy samples cannot claim an earlier observed breakout.
                    observations.append({"technical": technical, "scopeStart": technical["asOf"],
                                         "source": "recorded-analysis", "universeHash": None})
        except (ValueError, KeyError, TypeError):
            issues.append({"date": day, "error": "invalid-session-data"})
    return observations, history, sources, issues


def build_report(observations, history, profile, *, now):
    results, seen, errors = [], set(), []
    dates = sorted({r["technical"]["asOf"][:10] for r in observations if r["technical"]["asOf"] <= now.isoformat()})[-20:]
    closing = {(r["symbol"], r["date"]): r for r in history}
    # Prefer the explicit candidate watch over a legacy replay of the same bar.
    ordered = sorted(observations, key=lambda r: (r["source"] != "five-minute-observation", r["technical"]["asOf"]))
    for sample in ordered:
        t = sample["technical"]
        if t["asOf"][:10] not in dates or stamp(t["asOf"]) > now:
            continue
        try:
            signals, volume, cfg = evaluate_sample(t, history, profile, sample["scopeStart"])
            identity = (t["ticker"], signals["baseline"]["barEnd"], settings_key(cfg))
            if identity in seen:
                continue
            seen.add(identity)
            item = {"ticker": t["ticker"], "at": t["asOf"], "source": sample["source"],
                    "settingsHash": settings_key(t["settings"]), "universeHash": sample.get("universeHash"),
                    "effectiveCostRate": cfg["estimated_cost_rate"],
                    "timeVolume": volume, "results": {}}
            for strategy, signal in signals.items():
                unavailable = (strategy == "time_volume" and volume["ratio"] is None) or (
                    strategy == "breakout_retest" and sample["source"] == "recorded-analysis")
                if unavailable:
                    reason = "동일 시간대 거래량 표본 부족" if strategy == "time_volume" else "후보 관찰 시작 기록 부족"
                    item["results"][strategy] = {"allowed": None, "evaluated": False, "reasons": [reason], "path": {"outcome": "unavailable"}}
                    continue
                path = path_estimate(t, signal, cfg, closing.get((t["ticker"].split(".")[0], t["asOf"][:10])))
                item["results"][strategy] = {"allowed": signal["allowed"], "evaluated": True, "reasons": signal["reasons"], "path": path}
            results.append(item)
        except (ValueError, KeyError, TypeError, IndexError) as exc:
            errors.append({"ticker": t.get("ticker"), "at": t.get("asOf"), "error": type(exc).__name__})
    results.sort(key=lambda r: r["at"])
    totals = {}
    for name in EXPERIMENTS:
        rows = [r["results"][name] for r in results]
        returns = [number(r["path"]["estimatedNetReturn"]) for r in rows if r["path"].get("estimatedNetReturn") is not None]
        totals[name] = {"observations": len(rows), "evaluated": sum(r["evaluated"] for r in rows),
                        "signals": sum(r["allowed"] is True for r in rows),
                        "outcomes": dict(Counter(r["path"]["outcome"] for r in rows)),
                        "measuredPaths": len(returns), "meanEstimatedNetReturn": str(sum(returns) / len(returns)) if returns else None,
                        "reasons": Counter(v for r in rows for v in r["reasons"]).most_common(10)}
    return {"version": VERSION, "createdAt": now.isoformat(), "dates": dates, "costProfile": profile,
            "totals": totals, "observations": results, "errors": errors,
            "historyDays": len({r["date"] for r in history}),
            "sources": dict(Counter(r["source"] for r in results)),
            "timeVolumeReady": sum(r["timeVolume"]["status"] == "ready" for r in results),
            "promotionAllowed": False, "limitations": LIMITATIONS}


class ObservationPaused(Exception):
    pass


class ReadOnlyMarket:
    def __init__(self, backend, permitted=lambda: True):
        self.backend = backend
        self.clock = backend.clock
        self.permitted = permitted

    @property
    def market_cache_namespace(self):
        return getattr(self.backend, "market_cache_namespace", None)

    def call(self, method, *args, **kwargs):
        if method not in {"get_kr_calendar", "get_candles", "get_orderbook"}:
            raise ValueError("Research only permits market-data reads")
        if not self.permitted():
            raise ObservationPaused()
        return self.backend.call(method, *args, **kwargs)

    def calendar(self, day):
        return self.call("get_kr_calendar", day)


class StrategyComparisonService:
    def __init__(self, local_dir, mode, account, backend, settings, *, guard=lambda: True, clock=None):
        daily.state_path(local_dir, mode, account)
        self.local_dir, self.mode, self.account = Path(local_dir), mode, account
        self.root = self.local_dir / "strategy_research" / mode / account
        self.settings, self.guard = settings, guard
        self.backend = ReadOnlyMarket(backend, lambda: not self.closed and self.guard()
                                      and self.settings().get("strategy_observation", False))
        self.clock = clock or (lambda: datetime.now(KST))
        self.lock, self.thread = threading.RLock(), None
        self.active, self.error, self.closed = None, None, False
        self.watches, self.observed = [], set()

    def directory(self):
        archive_available()
        root = ARCHIVE_ROOT / self.mode / self.account
        if not root.resolve().is_relative_to(ARCHIVE_ROOT.resolve()):
            raise ValueError("Invalid research directory")
        return root

    def profile(self):
        return daily.read_json(self.root / "costs.json", {"revision": 0, "rates": cost_profile()})

    def save_profile(self, payload):
        profile = cost_profile(payload.get("rates"))
        with self.lock, weekly_review.locked(self.root):
            if self.active:
                raise ValueError("비교 완료 후 비용 기준을 변경하세요.")
            old = self.profile()
            if type(payload.get("revision")) is not int or payload["revision"] != old["revision"]:
                raise ValueError("비용 기준이 변경되었습니다. 새로고침하세요.")
            value = {"revision": old["revision"] + 1, "rates": profile, "createdAt": self.clock().isoformat(),
                     "scope": "research-only-paper-ledger-unchanged"}
            root = self.directory()
            daily.atomic_json(root / "costs" / f"{value['revision']:06d}.json", value)
            daily.atomic_json(self.root / "costs.json", value)
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            return {"available": True, "mode": self.mode, "active": self.active, "error": self.error,
                    "watching": [w["ticker"] for w in self.watches], "observationEnabled": self.settings().get("strategy_observation", False),
                    "profile": self.profile(), "paperCosts": paper_costs(self.local_dir) if self.mode == "dry-run" else None,
                    "report": daily.read_json(self.root / "latest.json", None)}

    def register(self, tickers, universe, settings):
        now = self.clock()
        valid = [t for t in tickers[:3] if re.fullmatch(r"[0-9A-Z]{6}\.KS", t) and t in universe.get("members", {})]
        with self.lock:
            self.watches = [{"ticker": t, "scopeStart": now.isoformat(), "expiresAt": (now + timedelta(minutes=60)).isoformat(),
                             "settingsHash": settings_key(settings), "universeHash": daily.digest(universe.get("members"))} for t in valid]
            self.observed.clear()

    def cancel(self):
        with self.lock:
            self.watches = []
            self.observed.clear()

    def tick(self, now):
        try:
            self._tick(now)
        except Exception as exc:
            # Research failure must not stop the trading/position scheduler.
            with self.lock:
                self.error = f"관찰 점검 실패 ({type(exc).__name__}). 매매 설정은 변경하지 않았습니다."

    def _tick(self, now):
        with self.lock:
            if self.closed or self.active:
                return
            cfg = self.settings()
            if not cfg.get("strategy_observation", False):
                self.cancel()
                return
            self.watches = [w for w in self.watches if stamp(w["expiresAt"]) > now
                            and stamp(w["scopeStart"]).date() == now.date() and w["settingsHash"] == settings_key(cfg)]
            if not self.watches or not self.guard():
                return
            bucket = now.replace(minute=now.minute // 5 * 5, second=0, microsecond=0).isoformat()
            pending = [w for w in self.watches if (w["ticker"], bucket) not in self.observed]
            if pending:
                self.observed.update((w["ticker"], bucket) for w in pending)
                self._start("observe", (deepcopy(pending), deepcopy(cfg)))

    def submit(self):
        with self.lock:
            self._start("compare", None)
        return self.snapshot()

    def _start(self, action, value):
        if self.active or self.closed:
            raise ValueError("전략 비교 작업이 진행 중입니다.")
        self.active, self.error = action, None
        self.thread = threading.Thread(target=self._work, args=(action, value), daemon=True)
        self.thread.start()

    def _work(self, action, value):
        try:
            root = self.directory()  # Check storage before any API request.
            if action == "observe":
                watches, cfg = value
                cfg["per_trade_budget"] = str(number(cfg.get("paper_capital", "10000000")) * number(cfg.get("position_pct", ".1")))
                for watch in watches:
                    with self.lock:
                        if (self.closed or watch not in self.watches or not self.guard()
                                or not self.settings().get("strategy_observation", False)
                                or stamp(watch["expiresAt"]) <= self.clock()
                                or settings_key(self.settings()) != watch["settingsHash"]):
                            break
                    technical = collect_technical(self.backend, watch["ticker"], str(self.clock().date()), cfg)
                    with self.lock:
                        if (self.closed or watch not in self.watches or not self.guard()
                                or not self.settings().get("strategy_observation", False)
                                or stamp(watch["expiresAt"]) <= self.clock()
                                or settings_key(self.settings()) != watch["settingsHash"]):
                            break
                    sample = {**watch, "technical": technical, "source": "five-minute-observation"}
                    daily.atomic_json(root / "observations" / technical["date"] / (daily.digest(sample) + ".json"), sample)
            else:
                now, profile = self.clock(), self.profile()
                observations, history, sources, issues = load_sources(self.mode, self.account, now)
                paths = sorted((root / "observations").glob("????-??-??/*.json"))
                if len(paths) > 500:
                    issues.append({"date": str(now.date()), "error": "500-observation-file-budget", "excluded": len(paths) - 500})
                total_bytes = 0
                for path in reversed(paths[-500:]):
                    try:
                        total_bytes += path.stat().st_size
                        if total_bytes > 32_000_000:
                            issues.append({"date": path.parent.name, "error": "32MB-observation-budget"})
                            break
                        sample = weekly_review.bounded_json(path, root)
                        if daily.digest(sample) != path.stem:
                            raise ValueError("observation-checksum")
                        if not isinstance(sample["technical"]["asOf"], str) or sample["source"] != "five-minute-observation":
                            raise ValueError("observation-schema")
                        observations.append(sample)
                    except (OSError, ValueError, KeyError, TypeError) as exc:
                        issues.append({"date": path.parent.name, "error": "observation-" + type(exc).__name__})
                observations.sort(key=lambda r: r["technical"]["asOf"])
                if len(observations) > 1000:
                    issues.append({"date": str(now.date()), "error": "1000-observation-budget"})
                    observations = observations[-1000:]
                report = build_report(observations, history, profile["rates"], now=now)
                engine_hash = daily.digest({p.name: p.read_text(encoding="utf-8") for p in (
                    Path(__file__), Path(__file__).parents[1] / "brokers/intraday.py", Path(candidate_review.__file__))})
                evidence = {"observations": observations, "history": history, "sources": sources, "issues": issues,
                            "profile": profile, "engineHash": engine_hash}
                identity = daily.digest({"version": VERSION, "evidence": evidence, "asOf": now.isoformat()})[:24]
                folder = root / "comparisons" / identity
                report.update(id=identity, mode=self.mode, account=self.account, costRevision=profile["revision"], engineHash=engine_hash,
                              sourceIssues=issues, status="partial" if issues or report["errors"] else
                              "completed" if report["observations"] else "insufficient-data",
                              reportPath=str(folder / "report.json"))
                daily.atomic_json(folder / "evidence.json", evidence)
                daily.atomic_json(folder / "report.json", report)
                daily.atomic_json(self.root / "latest.json", report)
        except ObservationPaused:
            pass
        except Exception as exc:
            with self.lock:
                self.error = f"{action} 실패 ({type(exc).__name__}): External 저장소·자료·호출 한도를 확인하세요. 매매 설정은 변경하지 않았습니다."
        finally:
            with self.lock:
                self.active = None

    def close(self):
        with self.lock:
            self.closed = True
            self.watches = []
        if self.thread:
            self.thread.join(timeout=100)

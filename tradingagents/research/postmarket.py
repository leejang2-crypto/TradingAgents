"""Post-close evidence review and bounded, mode/account-isolated risk learning.

No LLM, broker mutations, guessed fills, or model-weight training. Only complete
round trips with known costs and a recorded strategy context can teach sizing.
"""

from collections import Counter, defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from decimal import Decimal
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile

from tradingagents.brokers.intraday import INTRADAY_DEFAULTS, ENTRY_STRATEGIES, stamp, session_candles, market_reference
from tradingagents.brokers.paper_account import KST, number
from tradingagents.brokers.toss_transport import read_budget
from tradingagents.brokers.trading_hours import TIME_DEFAULTS
from . import candidate_review

VOLUME = Path("/Volumes/External")
ARCHIVE_ROOT = VOLUME / "TradingAgents" / "postmarket"
VERSION = "postmarket-v1"
DEFAULTS = {"postmarket_review": True, "postmarket_apply": True}
TERMINAL = {"FILLED", "SIMULATED_FILLED", "CANCELED", "REJECTED", "SIMULATED_REJECTED", "NOT_SENT"}
MIN_TRADES, MIN_DAYS = 10, 3


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(encode(value).encode()).hexdigest()


def atomic_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as stream:
            temp = Path(stream.name)
            stream.write(encode(payload) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if temp:
            temp.unlink(missing_ok=True)


def strategy_context(settings):
    # Risk allocation itself is excluded: a reduction must not erase its evidence.
    rules = {key: str(settings.get(key, value)) for key, value in INTRADAY_DEFAULTS.items()
             if key not in {"trade_risk_fraction", "daily_loss_limit", "max_consecutive_stops", "entry_watch_minutes"}}
    rules["analysis_engine"] = settings.get("intraday_engine", "rules-news")
    rules.update({key: str(settings.get(key, value)) for key, value in TIME_DEFAULTS.items()})
    return {"version": VERSION, "strategy": rules["entry_strategy"], "rules": rules, "fingerprint": digest(rules)}


def state_path(local_dir, mode, account):
    if mode not in {"live", "dry-run"} or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", account):
        raise ValueError("Invalid review identity")
    return Path(local_dir) / "postmarket" / mode / account / "latest.json"


def read_json(path, default=None):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default


def read_risk_state(path):
    if not path.exists():
        return {"orders": {}, "positions": {}, "days": {}}
    with sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10) as db:
        row = db.execute("SELECT payload FROM state WHERE id=1").fetchone()
    return json.loads(row[0]) if row else {"orders": {}, "positions": {}, "days": {}}


def reconstruct_trades(state, through):
    """Replay terminal cumulative orders once. Incomplete symbols never teach."""
    grouped = defaultdict(list)
    issues = []
    for key, order in state.get("orders", {}).items():
        try:
            if stamp(order["createdAt"]).date() > through:
                continue
            grouped[order["payload"]["symbol"]].append((key, order))
        except (KeyError, ValueError, TypeError):
            issues.append("invalid-order-metadata")
    completed, open_cycles = [], []
    for symbol, orders in grouped.items():
        qty, cost, proceeds = Decimal(0), Decimal(0), Decimal(0)
        cycle, trades = None, []
        try:
            for key, order in sorted(orders, key=lambda pair: (stamp(pair[1]["createdAt"]), pair[0])):
                if order.get("status") not in TERMINAL or order.get("financialsIncomplete"):
                    raise ValueError("unconfirmed-order-or-costs")
                filled = number(order.get("appliedQty", 0))
                if filled == 0:
                    continue
                detail = order["detail"]
                execution = detail["execution"]
                at = stamp(execution["filledAt"])
                if at.date() > through:
                    raise ValueError("later-fill-in-current-snapshot")
                if (filled <= 0 or filled != filled.to_integral_value()
                        or filled != number(execution["filledQuantity"])
                        or detail.get("status") != order.get("status")
                        or detail.get("symbol") != symbol or detail.get("side") != order["payload"]["side"]
                        or detail.get("currency") != "KRW" or detail.get("orderId") != order.get("orderId")):
                    raise ValueError("fill-identity-mismatch")
                requested = order["payload"].get("quantity")
                if requested is not None and (filled > number(requested)
                        or (order["status"] in {"FILLED", "SIMULATED_FILLED"} and filled != number(requested))):
                    raise ValueError("fill-quantity-mismatch")
                amount, fee, tax = (number(execution[field]) for field in ("filledAmount", "commission", "tax"))
                if amount <= 0 or min(fee, tax) < 0:
                    raise ValueError("invalid-fill-costs")
                if order["payload"]["side"] == "BUY":
                    if qty != 0:
                        raise ValueError("overlapping-buy-cycle")
                    cycle = {"id": key, "symbol": symbol, "entryAt": at.isoformat(),
                             "context": order.get("strategyContext"), "orderIds": [key]}
                    qty, cost, proceeds = filled, amount + fee + tax, Decimal(0)
                elif order["payload"]["side"] == "SELL":
                    if cycle is None or filled > qty or at < stamp(cycle["entryAt"]):
                        raise ValueError("unknown-basis-or-fill-order")
                    qty -= filled
                    proceeds += amount - fee - tax
                    cycle["orderIds"].append(key)
                    if qty == 0:
                        trades.append({**cycle, "exitAt": at.isoformat(), "cost": str(cost),
                                       "proceeds": str(proceeds), "netPnl": str(proceeds - cost),
                                       "netReturn": str(proceeds / cost - 1)})
                        cycle = None
                else:
                    raise ValueError("invalid-side")
            completed.extend(trades)
            if cycle:
                open_cycles.append({**cycle, "remainingQuantity": str(qty)})
        except (ValueError, TypeError, KeyError) as exc:
            # Do not assign a zero P&L or infer an unknown opening position's basis.
            issues.append(f"{symbol}: {exc}")
    return completed, open_cycles, issues


def derive_policy(trades, day, mode, account, *, usable=True):
    groups = defaultdict(list)
    for trade in trades:
        context = trade.get("context") or {}
        closed = stamp(trade["exitAt"]).date()
        if not day - timedelta(days=28) <= closed <= day:
            continue
        if (context.get("version") != VERSION or context.get("strategy") not in ENTRY_STRATEGIES
                or not isinstance(context.get("rules"), dict) or context.get("fingerprint") != digest(context["rules"])):
            continue
        groups[context["fingerprint"]].append(trade)
    adjustments, evidence = {}, []
    for fingerprint, rows in sorted(groups.items()):
        pnl = sum((number(row["netPnl"]) for row in rows), Decimal(0))
        wins = sum(number(row["netPnl"]) > 0 for row in rows)
        days = len({stamp(row["exitAt"]).date() for row in rows})
        sufficient = len(rows) >= MIN_TRADES and days >= MIN_DAYS
        reduce = usable and sufficient and pnl < 0 and wins / len(rows) < .4
        item = {"fingerprint": fingerprint, "strategy": rows[0]["context"]["strategy"],
                "trades": len(rows), "days": days, "wins": wins, "netPnl": str(pnl),
                "sufficient": sufficient, "multiplier": "0.75" if reduce else "1",
                "reason": "verified-loss-pattern" if reduce else "no-change"}
        evidence.append(item)
        if reduce:
            adjustments[fingerprint] = item
    return {"version": VERSION, "mode": mode, "account": account, "sourceDate": day.isoformat(),
            "effectiveFrom": (day + timedelta(days=1)).isoformat(),
            "expiresOn": (day + timedelta(days=7)).isoformat(), "adjustments": adjustments,
            "evidence": evidence, "usable": usable}


def sizing_adjustment(local_dir, mode, account, now, settings):
    baseline = {"multiplier": "1", "reason": "no-valid-review"}
    if not settings.get("postmarket_apply", True):
        return {**baseline, "reason": "disabled"}
    try:
        record = read_json(state_path(local_dir, mode, account), {})
        policy = record.get("policy", {})
        source = date.fromisoformat(policy["sourceDate"])
        if (policy.get("version") != VERSION or policy.get("mode") != mode or policy.get("account") != account
                or not policy.get("usable") or not source < now.date() <= source + timedelta(days=7)
                or policy.get("effectiveFrom") != (source + timedelta(days=1)).isoformat()
                or policy.get("expiresOn") != (source + timedelta(days=7)).isoformat()):
            return baseline
        context = strategy_context(settings)
        item = policy["adjustments"].get(context["fingerprint"])
        if (not item or item.get("multiplier") != "0.75" or item.get("strategy") != context["strategy"]
                or item.get("trades", 0) < MIN_TRADES or item.get("days", 0) < MIN_DAYS
                or number(item["netPnl"]) >= 0 or item["wins"] / item["trades"] >= .4):
            return baseline
        return {**item, "sourceDate": policy["sourceDate"], "reportPath": record.get("reportPath")}
    except (OSError, ValueError, TypeError, KeyError, ZeroDivisionError, AttributeError):
        # An unavailable/corrupt review cannot increase risk or break exit management.
        return baseline


def collect_market(backend, day):
    """Read-only closing index snapshot; no synthetic missing bars."""
    with read_budget(45):
        calendar = backend.calendar(day.isoformat())
        if calendar["today"]["date"] != day.isoformat():
            raise ValueError("Calendar date mismatch")
        session = (calendar["today"].get("integrated") or {}).get("regularMarket")
        if not session:
            return {"status": "closed", "calendar": calendar}
        start, end = stamp(session["startTime"]), stamp(session["endTime"])
        if start.date() != day or end.date() != day or end <= start:
            raise ValueError("Invalid regular session timestamps")
        if backend.clock() < end + timedelta(minutes=5):
            return {"status": "not-due", "dueAt": (end + timedelta(minutes=5)).isoformat()}
        markets = {}
        for name in ("KOSPI", "KOSDAQ"):
            rows = session_candles(backend.call, name, start, end, index=True,
                                   cache_namespace=getattr(backend, "market_cache_namespace", None))
            markets[name] = {"reference": market_reference(rows, start, end), "candles": rows}
        return {"status": "complete", "calendar": calendar, "indices": markets}


def collect_inputs(local_dir, mode, day, risk_path):
    local_dir = Path(local_dir).resolve()
    risk_path = Path(risk_path)
    if not risk_path.resolve().is_relative_to(local_dir / "positions"):
        raise ValueError("Risk ledger must belong to this workspace")
    prefix = "auto_live" if mode == "live" else "auto_paper"
    inputs = {"riskState": read_risk_state(risk_path), "summaries": [], "candidates": [], "replays": [], "experienceRuns": [], "manifest": []}
    paper_db = local_dir / "accounts/paper.sqlite3"
    if not paper_db.resolve().is_relative_to(local_dir / "accounts"):
        raise ValueError("Paper ledger leaves workspace")
    if mode == "dry-run" and paper_db.exists():
        with sqlite3.connect(paper_db.as_uri() + "?mode=ro", uri=True, timeout=10) as db:
            inputs["paperFills"] = [payload for (raw,) in db.execute("SELECT payload FROM fills ORDER BY recorded_at,rowid")
                                    if (payload := json.loads(raw)).get("recordedAt", "")[:10] <= str(day)]
    for category, folder, pattern in (("summaries", prefix, f"{prefix}_{day}_*.json"),
                                      ("candidates", "candidates", f"{prefix}_candidates_{day}_*.json")):
        for path in sorted((local_dir / folder).glob(pattern)):
            if not path.resolve().is_relative_to(local_dir) or path.is_symlink():
                raise ValueError("Review source leaves local workspace")
            payload = read_json(path)
            inputs[category].append(payload)
            inputs["manifest"].append({"path": str(path.relative_to(local_dir)), "sha256": digest(payload)})
    database = local_dir / "learning/experiences.sqlite3"
    if not database.resolve().is_relative_to(local_dir / "learning"):
        raise ValueError("Experience ledger leaves workspace")
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10) as db:
            db.row_factory = sqlite3.Row
            for row in db.execute("SELECT * FROM runs WHERE mode=? AND trade_date=? ORDER BY started_at", (mode, str(day))):
                run = dict(row)
                run["events"] = [{**dict(event), "payload": json.loads(event["payload"])}
                                 for event in db.execute("SELECT * FROM events WHERE run_id=? ORDER BY id", (run["id"],))]
                inputs["experienceRuns"].append(run)
                analyses = [event["payload"] for event in run["events"] if event["kind"] == "ANALYSIS"]
                planned = [event["payload"] for event in run["events"] if event["kind"] == "PLANNED"]
                result = [event["payload"] for event in run["events"] if event["kind"] == "RESULT"]
                failures = [event["payload"] for event in run["events"] if event["kind"] in {"ERROR", "INTERRUPTED"}]
                analysis = analyses[-1] if analyses else {}
                plan = planned[-1] if planned else {}
                inputs["summaries"].append({"executionMode": mode, "date": str(day), "source": "experience-store", "analyses": [{
                    "ticker": run["ticker"], "learning": {"runId": run["id"]}, "reportPath": plan.get("reportPath"),
                    "intradayContract": analysis.get("intradayContract"),
                    "toss": (result[-1].get("execution") if result else None) or {"plan": plan.get("plan", {})},
                    "error": failures[-1].get("error") if failures else None}]})
                inputs["manifest"].append({"path": f"learning/experiences.sqlite3#run={run['id']}", "sha256": digest(run)})
    # Only explicitly referenced reports, never arbitrary files or .env content.
    seen = set()
    for summary in inputs["summaries"]:
        for analysis in summary.get("analyses", []):
            report = analysis.get("reportPath")
            if not report:
                continue
            path = Path(report).with_name("intraday_replay.json")
            if path in seen or not path.resolve().is_relative_to(local_dir / "logs" / mode):
                continue
            seen.add(path)
            if path.exists():
                payload = read_json(path)
                inputs["replays"].append(payload)
                inputs["manifest"].append({"path": str(path.relative_to(local_dir)), "sha256": digest(payload)})
    return inputs


def analyze_day(inputs, day, mode, account):
    trades, open_cycles, issues = reconstruct_trades(inputs["riskState"], day)
    for fill in inputs.get("paperFills", []):
        if fill.get("status") == "SIMULATED_FILLED" and fill.get("id") not in inputs["riskState"].get("orders", {}):
            issues.append(f"paper-fill-not-reconciled: {fill.get('id')}")
    closed_today = [row for row in trades if stamp(row["exitAt"]).date() == day]
    actions, reasons, errors = Counter(), Counter(), []
    seen = set()
    stored_failures = Counter()
    for run in inputs.get("experienceRuns", []):
        failures = [event["payload"] for event in run["events"] if event["kind"] in {"ERROR", "INTERRUPTED"}]
        if failures:
            stored_failures[(run["ticker"], failures[-1].get("error"))] += 1
    for summary in inputs["summaries"]:
        if summary.get("executionMode", mode) != mode or summary.get("date") != day.isoformat():
            continue
        for row in summary.get("analyses", []):
            failure_key = (row.get("ticker"), row.get("error"))
            if (summary.get("source") != "experience-store" and row.get("error")
                    and not row.get("learning", {}).get("runId") and not row.get("reportPath")
                    and stored_failures[failure_key] > 0):
                stored_failures[failure_key] -= 1
                continue
            identity = row.get("learning", {}).get("runId") or row.get("reportPath") or digest(row)
            if identity in seen:
                continue
            seen.add(identity)
            contract = row.get("intradayContract") or {}
            action = contract.get("action") or (row.get("toss") or {}).get("plan", {}).get("action") or ("ERROR" if row.get("error") else "UNKNOWN")
            actions[action] += 1
            reasons.update(contract.get("reasons", []))
            if row.get("error"):
                errors.append({"ticker": row.get("ticker"), "error": row["error"]})
        if summary.get("selectionReason"):
            reasons[summary["selectionReason"]] += 1
    for snapshot in inputs["candidates"]:
        for row in snapshot.get("candidates", []):
            reasons.update((row.get("intraday") or {}).get("reasons", []))
    policy = derive_policy(trades, day, mode, account, usable=not issues and not inputs["riskState"].get("unconfirmed")
                           and inputs.get("market", {}).get("status") == "complete")
    return {"date": day.isoformat(), "mode": mode, "account": account,
            "roundTrips": len(closed_today), "wins": sum(number(row["netPnl"]) > 0 for row in closed_today),
            "netRealizedPnl": str(sum((number(row["netPnl"]) for row in closed_today), Decimal(0))) if not issues else None,
            "knownClosedPnl": str(sum((number(row["netPnl"]) for row in closed_today), Decimal(0))),
            "trades": closed_today, "openCycles": open_cycles, "dataIssues": issues,
            "actions": dict(actions), "blockReasons": reasons.most_common(20), "errors": errors,
            "policy": policy, "market": inputs.get("market", {"status": "unavailable"}),
            "candidateReview": candidate_review.analyze(inputs, day)}


def markdown(result):
    lines = [f"# 장 마감 복기: {result['date']} ({result['mode']})", "",
             f"- 확인된 완결 매매: {result['roundTrips']}회 / 이익 거래: {result['wins']}회",
             f"- 완결 매매 순손익: {result['netRealizedPnl'] if result['netRealizedPnl'] is not None else '확정 불가'} KRW",
             f"- 미청산 매매 주기: {len(result['openCycles'])}개", "",
             "순손익은 매수·매도 체결금액과 실제 기록 수수료·세금을 반영합니다. 부분 청산의 당일 실현손익,",
             "계좌 전체 평가손익/수익률과는 다릅니다. dry-run 수치는 모의 체결이며 실제 투자 성과가 아닙니다.", "",
             "## 시장 자료", f"- 종가 분봉 수집 상태: {result['market'].get('status')}"]
    for name, data in result["market"].get("indices", {}).items():
        reference = data["reference"]
        lines.append(f"- {name}: 최초 관측 봉 시가 대비 {reference['return'] * 100:+.2f}% (전일 종가 대비 등락 아님), 기준 {reference['basisStart']} ~ {reference['barEnd']}")
        if reference.get("warning"):
            lines.append(f"  - 자료 주의: {reference['warning']}")
    lines.extend(["", "## 종목별 완결 매매", "", "| 종목 | 진입 | 청산 | 방식 | 순손익(KRW) | 순수익률 |",
                  "|---|---|---|---|---:|---:|"])
    for trade in result["trades"]:
        strategy = (trade.get("context") or {}).get("strategy", "미기록")
        lines.append(f"| {trade['symbol']} | {trade['entryAt']} | {trade['exitAt']} | {strategy} | {trade['netPnl']} | {number(trade['netReturn']) * 100:.3f}% |")
    if not result["trades"]:
        lines.append("| 확인된 완결 매매 없음 | | | | | |")
    if result["openCycles"]:
        lines.extend(["", "## 잔여 보유"])
        lines.extend(f"- {row['symbol']}: {row['remainingQuantity']}주, 진입 {row['entryAt']}. 보유/청산 정책 점검 필요."
                     for row in result["openCycles"])
    lines.extend(["", "## 판단 및 차단 사유", f"- 판정 집계: {encode(result['actions'])}"])
    lines.extend(f"- {reason}: {count}회" for reason, count in result["blockReasons"])
    lines.extend(["", candidate_review.markdown(result["candidateReview"]), ""])
    lines.extend(["", "## 다음 매매 적용", "검증된 완결 매매 10건 이상·3거래일 이상·최근 28일·동일 규칙에서만 적용합니다.",
                  "누적 순손실이면서 승률 40% 미만일 때 해당 규칙의 신규 투자금/위험 예산을 75%로 줄입니다.",
                  "익절·손절·진입 조건·일일 손실 한도는 변경하지 않습니다. 이 변경의 성과 개선은 보장되지 않습니다."])
    for item in result["policy"]["evidence"]:
        lines.append(f"- {item['strategy']} / 표본 {item['trades']}건·{item['days']}일 / 순손익 {item['netPnl']}원 / 신규 진입 규모 배수 {item['multiplier']}")
    if not result["policy"]["adjustments"]:
        lines.append("- 적용 변경 없음: 표본 부족, 손실 조건 미충족 또는 체결자료 미확인.")
    if result["errors"]:
        lines.append("- 분석 오류가 기록되어 있습니다. 매매 조건 완화보다 모델 응답/원문 근거/시장자료 오류 점검이 우선입니다.")
    if not result["roundTrips"]:
        lines.append("- 무거래일: 가상 가격 구간은 별도 참고자료이며 실제 손익/승률/규모 조정에 포함하지 않습니다.")
    lines.extend(["", "## 한계와 검증 과제", "- HOLD 이후 상승은 체결 가능한 수익의 증거가 아닙니다. 놓친 수익을 가상의 학습 보상으로 쓰지 않습니다.",
                  "- 이 복기는 통계 기반 규칙 반영이며 LLM 가중치 학습이나 검증된 최적 전략 탐색이 아닙니다.",
                  "- 원자료/미체결/잔여 보유/데이터 누락을 구분하며, 무거래일도 분석·차단 사유를 기록합니다."])
    lines.extend(f"- 자료 확인 필요: {issue}" for issue in result["dataIssues"])
    lines.extend(f"- 분석 오류: {row['ticker']}: {row['error']}" for row in result["errors"])
    return "\n".join(lines) + "\n"


def archive_available():
    # Never create a fake /Volumes/External directory on the internal SSD.
    if not VOLUME.is_mount() or not ARCHIVE_ROOT.resolve().is_relative_to(VOLUME.resolve()):
        raise OSError("External drive is not mounted or archive path is invalid")
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)


def cached_candidate_prices(record, mode, account, day):
    if record.get("date") != day.isoformat() or record.get("mode") != mode or record.get("account") != account:
        return {}
    try:
        path = Path(record["directory"]) / "inputs.json.gz"
        if not path.resolve().is_relative_to((ARCHIVE_ROOT / mode / account / day.isoformat()).resolve()):
            return {}
        with gzip.open(path, "rt", encoding="utf-8") as stream:
            return json.load(stream).get("candidatePrices", {})
    except (OSError, ValueError, TypeError, KeyError, AttributeError, EOFError):
        return {}


def run_review(local_dir, mode, day, risk_path, *, now=None, market=None, backend=None):
    now = now or datetime.now(KST)
    if day > now.date() or (day == now.date() and now.time() < datetime.strptime("15:35", "%H:%M").time()):
        raise ValueError("Postmarket review runs after the regular session, never before close")
    account = Path(risk_path).stem
    if (mode == "dry-run") != (account == "paper"):
        raise ValueError("Review account/mode mismatch")
    path = state_path(local_dir, mode, account)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.with_suffix(".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        archive_available()
        previous = read_json(path, {})
        if not isinstance(previous, dict):
            previous = {}
        attempts = previous.get("candidateFetchAttempts", 0) if previous.get("date") == day.isoformat() else 0
        if type(attempts) is not int or attempts < 0:
            attempts = 0
        inputs = collect_inputs(local_dir, mode, day.isoformat(), Path(risk_path))
        inputs["market"] = market or {"status": "unavailable", "reason": "offline-review-no-closing-fetch"}
        inputs["candidatePrices"] = candidate_review.collect_prices(inputs, day, backend if attempts < 3 else None,
            cached_candidate_prices(previous, mode, account, day))
        if backend is not None and inputs["market"].get("status") == "complete" and inputs["candidatePrices"] and attempts < 3:
            attempts += 1
        candidate_pending = any(row.get("status") != "complete" and row.get("reason") != "candidate-time-or-settings-missing"
                                for row in inputs["candidatePrices"].values())
        result = analyze_day(inputs, day, mode, account)
        revision = digest({"version": VERSION, "candidateVersion": candidate_review.VERSION, "inputs": inputs})[:20]
        destination = ARCHIVE_ROOT / mode / account / day.isoformat() / revision
        if not destination.resolve().is_relative_to(ARCHIVE_ROOT.resolve()):
            raise ValueError("Archive destination leaves external folder")
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            staging = Path(tempfile.mkdtemp(prefix=".review-", dir=destination.parent))
            try:
                (staging / "review.md").write_text(markdown(result), encoding="utf-8")
                atomic_json(staging / "review.json", result)
                atomic_json(staging / "policy.json", result["policy"])
                atomic_json(staging / "opportunities.json", result["candidateReview"])
                with gzip.open(staging / "inputs.json.gz", "wt", encoding="utf-8") as stream:
                    stream.write(encode(inputs))
                atomic_json(staging / "manifest.json", {"version": VERSION, "date": day.isoformat(), "mode": mode,
                    "account": account, "revision": revision, "createdAt": now.isoformat(), "sources": inputs["manifest"]})
                for file in staging.iterdir():
                    file.chmod(0o600)
                os.rename(staging, destination)
            except BaseException:
                # Keep incomplete staging for diagnosis; never publish its policy.
                raise
        record = {"date": day.isoformat(), "mode": mode, "account": account, "updatedAt": now.isoformat(),
                  "status": "partial" if result["dataIssues"] or inputs["market"].get("status") != "complete"
                            or result["candidateReview"]["status"] != "complete" else "completed",
                  "candidateFetchAttempts": attempts,
                  "reviewFinished": not result["dataIssues"] and inputs["market"].get("status") == "complete"
                                    and (not candidate_pending or attempts >= 3),
                  "reportPath": str(destination / "review.md"), "directory": str(destination),
                  "roundTrips": result["roundTrips"], "netRealizedPnl": result["netRealizedPnl"],
                  "candidateReview": candidate_review.compact(result["candidateReview"]),
                  "actions": result["actions"], "policy": result["policy"], "dataIssues": result["dataIssues"]}
        # An explicit old-date review may be archived, but cannot replace a newer policy.
        offline_downgrade = (previous.get("date") == day.isoformat() and previous.get("status") == "completed"
                             and inputs["market"].get("reason") == "offline-review-no-closing-fetch")
        if previous.get("date", "") <= day.isoformat() and not offline_downgrade:
            atomic_json(path, record)
            atomic_json(Path(local_dir) / "postmarket" / f"latest-{mode}.json", record)
        return record


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Offline postmarket review; no orders or LLM/API requests")
    parser.add_argument("--local-dir", type=Path, default=Path(__file__).resolve().parents[2] / ".local")
    parser.add_argument("--date", required=True, type=date.fromisoformat)
    parser.add_argument("--mode", choices=["dry-run", "live"], default="dry-run")
    parser.add_argument("--risk-db", type=Path)
    args = parser.parse_args()
    if args.mode == "live" and not args.risk_db:
        parser.error("Live review requires an explicit account risk database")
    result = run_review(args.local_dir, args.mode, args.date, args.risk_db or args.local_dir / "positions/paper.sqlite3")
    print(encode(result))


if __name__ == "__main__":
    main()

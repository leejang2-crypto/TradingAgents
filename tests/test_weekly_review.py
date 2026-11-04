from copy import deepcopy
from datetime import date, datetime, timedelta
from decimal import Decimal
import gzip
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

import main
from tradingagents.research import postmarket as daily, weekly_review as weekly, candidate_review
from tradingagents.brokers.position_risk import RiskStore
from tradingagents.brokers.toss_executor import TossOrderPlan
from tests.test_postmarket import order, publish_policy
from tests import test_position_risk, test_dashboard_auth
from tests.test_dashboard_auth import login, request

env = test_position_risk.env
server = test_dashboard_auth.server


NOW = datetime(2026, 10, 31, 10, tzinfo=main.KOREA_TZ)


class Calendar:
    def __init__(self, holidays=()):
        self.holidays, self.calls = set(holidays), []

    def calendar(self, day):
        self.calls.append(day)
        today = date.fromisoformat(day)
        previous = today - timedelta(days=1)
        while previous.weekday() >= 5 or previous in self.holidays:
            previous -= timedelta(days=1)
        opened = datetime.combine(today, datetime.min.time(), main.KOREA_TZ).replace(hour=9)
        regular = {"startTime": opened.isoformat(), "endTime": opened.replace(hour=15, minute=30).isoformat()}
        return {"today": {"date": day, "integrated": {"regularMarket": regular} if today.weekday() < 5 and today not in self.holidays else None},
                "previousBusinessDay": {"date": str(previous)}}


def write_day(root, day, state, *, when=None, extra=None):
    inputs = {"riskState": deepcopy(state), "market": {"status": "complete", "calendar": Calendar().calendar(str(day))},
              "summaries": [], "candidates": [], "replays": [], "experienceRuns": [], "manifest": [], **(extra or {})}
    result = daily.analyze_day(inputs, day, "dry-run", "paper")
    revision = daily.digest({"version": daily.VERSION, "candidateVersion": candidate_review.VERSION, "inputs": inputs})[:20]
    directory = root / "dry-run/paper" / str(day) / revision
    directory.mkdir(parents=True, exist_ok=True)
    daily.atomic_json(directory / "review.json", result)
    with gzip.open(directory / "inputs.json.gz", "wt", encoding="utf-8") as stream:
        json.dump(inputs, stream)
    daily.atomic_json(directory / "manifest.json", {"version": daily.VERSION, "mode": "dry-run", "account": "paper",
        "date": str(day), "revision": revision, "createdAt": (when or NOW).isoformat()})
    return directory


@pytest.fixture
def bundle(tmp_path, monkeypatch):
    volume = tmp_path / "External"
    volume.mkdir()
    monkeypatch.setattr(daily, "VOLUME", volume)
    monkeypatch.setattr(daily, "ARCHIVE_ROOT", volume / "TradingAgents/postmarket")
    monkeypatch.setattr(weekly, "ARCHIVE_ROOT", volume / "TradingAgents/weekly_review")
    original = Path.is_mount
    monkeypatch.setattr(Path, "is_mount", lambda p: p == volume or original(p))
    local = tmp_path / "local"
    risk = local / "positions/paper.sqlite3"
    store = RiskStore(risk)
    with store.locked():
        store.load()
        store.save({"orders": {}, "positions": {}, "days": {}})
    state = {"orders": {}, "positions": {}, "days": {}}
    days = [date(2026, 10, 19) + timedelta(days=i) for i in range(12) if (date(2026, 10, 19) + timedelta(days=i)).weekday() < 5]
    for i, day in enumerate(days):
        for j in range(3):
            index = i * 3 + j
            at = datetime.combine(day, datetime.min.time(), main.KOREA_TZ).replace(hour=10+j)
            symbol, key = f"{index+1:06d}", str(index)
            context = {**daily.strategy_context({}), "weeklySettingsKey": weekly.settings_key({})}
            state["orders"]["b" + key] = order("b" + key, symbol, "BUY", at, "1000", context=context)
            state["orders"]["s" + key] = order("s" + key, symbol, "SELL", at + timedelta(minutes=30), "980", tax="2")
        write_day(daily.ARCHIVE_ROOT, day, state)
    window = weekly.calendar_window(Calendar(), NOW)
    return local, risk, window, state


def run(bundle):
    local, risk, window, _ = bundle
    return weekly.run_review(local, "dry-run", risk, {}, window, now=NOW)


def test_calendar_weekend_and_weekday_holiday():
    backend = Calendar([date(2026, 10, 29)])
    result = weekly.calendar_window(backend, NOW)
    assert result["closed"] and result["days"] == ['2026-10-23', '2026-10-26', '2026-10-27', '2026-10-28', '2026-10-30']
    holiday = weekly.calendar_window(backend, NOW.replace(day=29))
    assert holiday["closed"] and holiday["days"][-1] == '2026-10-28'
    backend.calls.clear()
    opened = weekly.calendar_window(backend, NOW.replace(day=30), closed_only=True)
    assert not opened["closed"] and len(backend.calls) == 1


@pytest.mark.parametrize("bad", ["date", "chain", "session"])
def test_calendar_corruption_fails_closed(bad):
    backend = Calendar()
    original = backend.calendar
    def corrupt(day):
        result = original(day)
        if bad == "date": result["today"]["date"] = "2000-01-01"
        if bad == "chain": result["previousBusinessDay"]["date"] = day
        if bad == "session": result["today"]["integrated"] = {"regularMarket": {"startTime": day + "T15:30:00+09:00", "endTime": day + "T09:00:00+09:00"}}
        return result
    backend.calendar = corrupt
    with pytest.raises(ValueError): weekly.calendar_window(backend, NOW)


def test_weekly_archive_dedup_history_permissions_and_no_secret(bundle):
    local, risk, _, _ = bundle
    (local / ".env").write_text("TOKEN=NEVER_EXPORT_THIS")
    result = run(bundle)
    assert result["diagnostics"]["actual"]["trades"] == 15
    assert result["history"]["trades"] == 30
    assert result["proposal"]["status"] == "proposed"
    assert result["diagnostics"]["actual"]["netPnl"] == "-360"
    assert result["status"] == "completed"
    repeat = weekly.run_review(local, "dry-run", risk, {}, weekly.calendar_window(Calendar(), NOW + timedelta(days=1)), now=NOW + timedelta(days=1))
    assert repeat["id"] == result["id"] and repeat["unchanged"]
    for path in Path(result["directory"]).iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert "NEVER_EXPORT_THIS" not in path.read_text()


def test_latest_daily_revision_only(bundle):
    _, _, window, state = bundle
    changed = deepcopy(state)
    changed["orders"]["s29"]["detail"]["execution"]["filledAmount"] = "990"
    write_day(daily.ARCHIVE_ROOT, date.fromisoformat(window["days"][-1]), changed, when=NOW + timedelta(minutes=1))
    result = run(bundle)
    assert result["diagnostics"]["actual"]["trades"] == 15
    assert result["diagnostics"]["actual"]["netPnl"] == "-350"


def test_missing_day_falls_back_to_local_and_blocks_approval(bundle, monkeypatch):
    collect = weekly.collect_archives
    def missing(*args):
        records, sources, issues = collect(*args)
        del records['2026-10-30']
        return records, sources, issues
    monkeypatch.setattr(weekly, "collect_archives", missing)
    result = run(bundle)
    assert result["status"] == "partial" and result["missingDays"] == ['2026-10-30']
    root = weekly.state_dir(bundle[0], 'dry-run', 'paper')
    assert not weekly.compare(root, result["id"])["eligible"]


def test_corrupt_newest_revision_is_not_silently_replaced(bundle):
    path = next((daily.ARCHIVE_ROOT / 'dry-run/paper/2026-10-30').glob('*/review.json'))
    payload = daily.read_json(path)
    payload['trades'][0]['netPnl'] = '900000000'
    daily.atomic_json(path, payload)
    result = run(bundle)
    assert result['status'] == 'partial' and result['sourceIssues']
    assert number(result['diagnostics']['actual']['netPnl']) <= 0


def number(value):
    return Decimal(value)


def test_unmounted_drive_does_not_create_fallback(bundle, monkeypatch):
    monkeypatch.setattr(Path, 'is_mount', lambda p: False)
    with pytest.raises(OSError): run(bundle)
    assert not weekly.ARCHIVE_ROOT.exists()


def test_paired_compare_approval_runtime_and_rollback(bundle):
    result = run(bundle)
    root = weekly.state_dir(bundle[0], 'dry-run', 'paper')
    compared = weekly.compare(root, result['id'], now=NOW)
    assert compared['eligible'] and compared['baseline']['trades'] == compared['candidate']['trades'] == 12
    assert compared['baseline']['netPnl'] == '-288'
    assert number(compared['candidate']['netPnl']) > number(compared['baseline']['netPnl'])
    assert weekly.sizing_adjustment(bundle[0], 'dry-run', 'paper', NOW, {})['multiplier'] == '1'
    with pytest.raises(ValueError): weekly.approve(root, result['id'], {}, 'dry-run', 'paper', now=NOW)
    policy = weekly.approve(root, result['id'], {}, 'dry-run', 'paper', confirmed=True, now=NOW)
    assert weekly.sizing_adjustment(bundle[0], 'dry-run', 'paper', NOW, {})['multiplier'] == '0.75'
    assert weekly.sizing_adjustment(bundle[0], 'live', 'another', NOW, {})['multiplier'] == '1'
    for settings in ({'position_stop_loss': '.01'}, {'paper_capital': '50000000'}, {'position_pct': '.15'}, {'provider': 'ollama'}):
        assert weekly.sizing_adjustment(bundle[0], 'dry-run', 'paper', NOW, settings)['multiplier'] == '1'
    assert weekly.sizing_adjustment(bundle[0], 'dry-run', 'paper', NOW + timedelta(days=8), {})['multiplier'] == '1'
    assert weekly.sizing_adjustment(bundle[0], 'dry-run', 'paper', NOW - timedelta(days=1), {})['multiplier'] == '1'
    with pytest.raises(ValueError): weekly.rollback(root, 0, confirmed=True)
    weekly.rollback(root, policy['revision'], confirmed=True, now=NOW)
    assert weekly.sizing_adjustment(bundle[0], 'dry-run', 'paper', NOW, {})['multiplier'] == '1'
    assert len(list((root / 'history').glob('*.json'))) == 2


@pytest.mark.parametrize('bad', ['report-id', 'settings', 'expired', 'comparison'])
def test_stale_approval_is_rejected(bundle, bad):
    result = run(bundle)
    root = weekly.state_dir(bundle[0], 'dry-run', 'paper')
    weekly.compare(root, result['id'])
    report_id, settings, now = result['id'], {}, NOW
    if bad == 'report-id': report_id = 'stale'
    if bad == 'settings': settings = {'min_relative_volume': '3'}
    if bad == 'expired': now += timedelta(days=9)
    if bad == 'comparison': daily.atomic_json(root / 'comparison.json', {'eligible': True})
    with pytest.raises(ValueError): weekly.approve(root, report_id, settings, 'dry-run', 'paper', confirmed=True, now=now)


def test_simulation_cash_quantity_fees_and_partial_sells():
    at = NOW.replace(day=30, hour=10)
    orders = {'b': order('b', '005930', 'BUY', at, '1000'),
              's1': order('s1', '005930', 'SELL', at + timedelta(minutes=10), '490', quantity='5'),
              's2': order('s2', '005930', 'SELL', at + timedelta(minutes=20), '490', quantity='5')}
    trades, _, _ = daily.reconstruct_trades({'orders': orders}, at.date())
    baseline = weekly.replay_sizing(trades, orders, '10000', '1')
    reduced = weekly.replay_sizing(trades, orders, '10000', '.75')
    assert baseline['netPnl'] == '-23' and reduced['netPnl'] == '-17'
    assert not reduced['openPositions'] and not reduced['skipped']
    assert number(reduced['closedPnlDrawdown']) == 17
    assert weekly.replay_sizing(trades, orders, '10', '1')['skipped'] == ['b']


def test_scheduler_checks_holidays_dedup_notifies_only_new(bundle):
    local, risk, _, _ = bundle
    backend, notifier = Calendar(), Mock()
    now = [NOW]
    service = weekly.WeeklyReviewService(local, 'dry-run', risk, backend, lambda: {}, notifier, lambda: now[0])
    service.tick(NOW.replace(hour=8))
    assert not service.active and not backend.calls
    service.tick(NOW)
    service.thread.join(10)
    assert service.snapshot()['report'] and not service.error
    assert notifier.emit.call_count == 1
    calls = len(backend.calls)
    service.tick(NOW + timedelta(minutes=5))
    assert len(backend.calls) == calls
    now[0] += timedelta(days=1)
    service.tick(now[0])
    service.thread.join(10)
    assert notifier.emit.call_count == 1
    service.close()


def test_scheduler_calendar_failure_does_not_touch_trading(bundle):
    local, risk, _, _ = bundle
    backend = Mock()
    backend.calendar.side_effect = TimeoutError('do not expose secrets')
    before = risk.read_bytes()
    service = weekly.WeeklyReviewService(local, 'dry-run', risk, backend, lambda: {}, clock=lambda: NOW)
    service.tick(NOW)
    service.thread.join(5)
    assert 'TimeoutError' in service.error and 'secrets' not in service.error
    assert service.next_check == NOW + timedelta(minutes=15)
    assert risk.read_bytes() == before
    backend.execute.assert_not_called()
    service.close()


def test_scheduler_disabled_and_open_market(bundle):
    local, risk, _, _ = bundle
    backend = Calendar()
    settings = {'weekly_review': False}
    service = weekly.WeeklyReviewService(local, 'dry-run', risk, backend, lambda: settings, clock=lambda: NOW.replace(day=30))
    service.tick(NOW)
    assert not backend.calls
    settings['weekly_review'] = True
    service.tick(NOW.replace(day=30))
    service.thread.join(5)
    assert len(backend.calls) == 1 and service.snapshot()['report'] is None
    service.close()


def test_weekly_sizing_combines_by_minimum_without_changing_exit(env):
    manager, broker, times, settings = env
    root = weekly.state_dir(manager.local_dir, 'live', manager.store.path.stem)
    source = times[0].date() - timedelta(days=1)
    daily.atomic_json(root / 'policy.json', {'version': weekly.VERSION, 'revision': 1, 'status': 'approved', 'mode': 'live',
        'account': manager.store.path.stem, 'fingerprint': daily.strategy_context(settings)['fingerprint'],
        'settingsKey': weekly.settings_key(settings), 'multiplier': '0.75', 'reportId': 'fixture', 'sourceDate': str(source),
        'expiresOn': str(source + timedelta(days=7)), 'approvedAt': (times[0] - timedelta(minutes=1)).isoformat()})
    publish_policy(manager.local_dir, 'live', manager.store.path.stem, settings, source)
    result = manager.execute(TossOrderPlan(action='BUY', rating='Buy', symbol='005930', order_amount='1000'))
    assert result['payload']['quantity'] == '7'
    saved = next(iter(manager.snapshot()['orders'].values()))
    assert saved['strategyContext']['weeklySizingAdjustment']['multiplier'] == '0.75'
    assert settings['position_target_return'] == '.01'


def test_weekly_http_auth_csrf_and_manual_lane_isolation(server, tmp_path, monkeypatch):
    service = weekly.WeeklyReviewService(tmp_path, 'dry-run', tmp_path / 'positions/paper.sqlite3', Calendar(), lambda: {})
    server.weekly = service
    submitted = Mock(return_value={'available': True})
    monkeypatch.setattr(service, 'submit', submitted)
    assert request(server, 'GET', '/api/weekly')[0] == 401
    cookie = login(server)
    assert request(server, 'GET', '/api/weekly', cookie=cookie)[0] == 200
    before = server.control.snapshot()
    for action in ('run', 'compare', 'summary', 'approve', 'rollback'):
        assert request(server, 'POST', '/api/weekly/' + action, {})[0] == 401
        assert request(server, 'POST', '/api/weekly/' + action, {}, cookie=cookie, extra={'Origin': 'https://evil.test'})[0] == 403
        assert request(server, 'POST', '/api/weekly/' + action, {}, cookie=cookie)[0] == 202
    assert submitted.call_count == 5 and before == server.control.snapshot()
    assert request(server, 'GET', '/assets/weekly.js')[0] == 200


def test_setting_validation():
    assert main.validate_trading_settings({'weekly_review': False}) == {'weekly_review': False}
    with pytest.raises(ValueError): main.validate_trading_settings({'weekly_review': 'true'})
    assert main.make_parser().parse_args(['--no-weekly-review']).weekly_review is False


def test_hold_counts_losses_as_well_as_opportunities_without_double_counting():
    candidate = {'status': 'complete', 'windows': []}
    for outcome in ('target', 'target', 'stop', 'ambiguous'):
        candidate['windows'].append({'strategy': 'breakout', 'outcome': outcome, 'configuredStrategy': True,
            'marketCheck': 'passed', 'signalAt': '2026-10-30T10:05:00+09:00',
            'recordedDecision': {'action': 'HOLD', 'at': '2026-10-30T10:00:00+09:00'}})
    item = {'trades': [], 'inputs': {'riskState': {'orders': {}}},
            'result': {'candidateReview': {'candidates': [candidate]}}}
    result = weekly.diagnostics({'2026-10-30': item}, ['2026-10-30'])
    assert result['hold'] == {'evaluatedCandidateDays': 1, 'targetAfterHold': 1, 'stopAfterHold': 1, 'ambiguousAfterHold': 1}
    assert result['patterns']['breakout']['target'] == 2
    candidate['windows'][0]['recordedDecision']['at'] = '2026-10-30T11:00:00+09:00'
    candidate['windows'] = candidate['windows'][:1]
    assert not weekly.diagnostics({'2026-10-30': item}, ['2026-10-30'])['hold']


def test_old_trades_without_full_settings_stay_diagnostic_only(bundle):
    records, sources, issues = weekly.collect_archives('dry-run', 'paper', bundle[2]['days'])
    for row in records.values():
        for trade in row['trades']:
            trade['context'].pop('weeklySettingsKey')
    report, evidence = weekly.build_report(records, sources, issues, bundle[2], 'dry-run', 'paper', {})
    assert report['history']['trades'] == 30 and report['diagnostics']['actual']['trades'] == 15
    assert report['proposal']['status'] == 'insufficient-evidence' and evidence['trades'] == []


@pytest.mark.parametrize('ids,valid', [([0, 1, 2], True), ([0, 0, 1], False), ([0, 1, 999], False), ([[0], 1, 2], False), ([True, 1, 2], False)])
def test_local_summary_can_only_select_recorded_facts(bundle, monkeypatch, ids, valid):
    import httpx
    report = run(bundle)
    response = Mock()
    response.content = b'{}'
    response.json.return_value = {'message': {'content': json.dumps({'ids': ids, 'summary': 'UNSUPPORTED PROFIT CLAIM'})}}
    client = Mock()
    client.__enter__ = Mock(return_value=client)
    client.__exit__ = Mock(return_value=None)
    client.post.return_value = response
    factory = Mock(return_value=client)
    monkeypatch.setattr(httpx, 'Client', factory)
    if valid:
        result = weekly.local_summary(report, {})
        assert result['kind'] == 'extractive-facts' and 'UNSUPPORTED' not in json.dumps(result)
    else:
        with pytest.raises(ValueError): weekly.local_summary(report, {})
    assert factory.call_args.kwargs['trust_env'] is False
    assert factory.call_args.kwargs['follow_redirects'] is False
    assert client.post.call_args.args[0].startswith('http://localhost:')


def test_local_summary_rejects_cloud_and_remote_endpoint(bundle):
    report = run(bundle)
    with pytest.raises(ValueError): weekly.local_summary(report, {'ollama_quick_model': 'model:cloud'})
    with pytest.raises(ValueError): weekly.local_summary(report, {'ollama_base_url': 'https://external.example/v1'})


def test_corrupt_policy_never_changes_sizing_or_breaks_snapshot(bundle):
    local, risk, _, _ = bundle
    root = weekly.state_dir(local, 'dry-run', 'paper')
    root.mkdir(parents=True)
    (root / 'policy.json').write_text('{broken')
    service = weekly.WeeklyReviewService(local, 'dry-run', risk, Calendar(), lambda: {}, clock=lambda: NOW)
    assert service.snapshot()['policy'] is None
    assert service.snapshot()['effectiveSizing']['multiplier'] == '1'


@pytest.mark.parametrize('kind', ['unconfirmed', 'unknown-holding'])
def test_unconfirmed_ledger_or_unmatched_holdings_block_approval(bundle, kind):
    state = deepcopy(bundle[3])
    if kind == 'unconfirmed': state['unconfirmed'] = True
    else: state['positions']['999999'] = {'quantity': '20'}
    write_day(daily.ARCHIVE_ROOT, date(2026, 10, 30), state, when=NOW + timedelta(minutes=1))
    report = run(bundle)
    assert report['status'] == 'partial'
    compared = weekly.compare(weekly.state_dir(bundle[0], 'dry-run', 'paper'), report['id'])
    assert not compared['eligible'] and not compared['checks']['sourceComplete']

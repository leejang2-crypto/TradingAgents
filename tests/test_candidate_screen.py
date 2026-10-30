import asyncio
from copy import deepcopy
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
import main
from tests.test_intraday import data
from tests.test_long_term_research import universe_snapshot
from tradingagents.brokers.candidate_screen import measure_candidate, screen_candidates
from tradingagents.monitoring.control import RunStopped
from tradingagents.brokers.toss_client import TossApiError


class Market:
    def __init__(self, data):
        self.rows, _, _, self.start, self.now = data
        self.calls = []
        self.fail = {}

    def call(self, method, symbol, **kwargs):
        self.calls.append((method, symbol))
        if symbol in self.fail:
            raise self.fail[symbol]
        if method == 'get_kr_calendar':
            return {'today': {'date': symbol, 'integrated': {'regularMarket': {
                'startTime': self.start.isoformat(), 'endTime': self.start.replace(hour=15, minute=30).isoformat()}}}}
        rows = deepcopy(self.rows)
        if symbol == 'KOSPI':
            for row in rows:
                row.update(openPrice='10000', closePrice='10000', highPrice='10010', lowPrice='9990')
                from datetime import datetime
                row['timestamp'] = (datetime.fromisoformat(row['timestamp']) - timedelta(minutes=1)).isoformat()
        if symbol == '000660':
            for row in rows[-5:]:
                row.update(volume='10000', closePrice='10100', highPrice='10110')
        return {'candles': rows, 'nextBefore': None}


def candidates():
    return [{'ticker': '005930.KS', 'name': 'Samsung', 'score': 100},
            {'ticker': '000660.KS', 'name': 'SK', 'score': 1}]


def screen(data, backend=None, **kwargs):
    market = backend or Market(data)
    return screen_candidates(candidates(), market, {'min_session_turnover': '1000000', 'buy_start_minutes': 20},
                             requested_date=data[4].date().isoformat(), clock=lambda: market.now, **kwargs)


def test_market_ranking_can_overturn_news_order_and_shares_index(data):
    market = Market(data)
    result = screen(data, market)
    assert result['eligibleCount'] == 2
    assert result['rows'][0]['ticker'] == '000660.KS'
    assert result['rows'][0]['newsRank'] == 2
    assert market.calls.count(('get_candles', 'KOSPI')) == 1
    assert all(name in {'get_candles', 'get_kr_calendar'} for name, _ in market.calls)
    check = result['rows'][0]['intraday']
    assert 0 <= check['score'] <= 100 and sum(check['components'].values()) == pytest.approx(check['score'])


@pytest.mark.parametrize('case', ['missing', 'weak', 'volume', 'vwap', 'volatility'])
def test_bad_or_unsuitable_candles_do_not_pass(data, case):
    rows, _, _, start, now = deepcopy(data)
    if case == 'missing': rows.pop(4)
    if case == 'volume':
        for row in rows[-5:]: row['volume'] = '10'
    if case == 'vwap':
        for row in rows[:15]: row.update(openPrice='11000', closePrice='11000', highPrice='11100', lowPrice='10900')
    if case == 'volatility':
        for row in rows: row.update(highPrice='12000', lowPrice='8000')
    if case == 'missing':
        with pytest.raises(ValueError): measure_candidate(rows, start, now, 0, {})
    else:
        result = measure_candidate(rows, start, now, .02 if case == 'weak' else 0, {'min_session_turnover': '1000000'})
        assert not result['eligible'] and result['reasons']


def test_missing_symbol_data_is_excluded_without_fallback(data):
    market = Market(data)
    market.fail['005930'] = ValueError('missing candles')
    result = screen(data, market)
    assert result['status'] == 'partial' and result['eligibleCount'] == 1
    assert result['rows'][-1]['intraday']['score'] is None


@pytest.mark.parametrize('code', [401, 403, 429])
def test_rate_or_auth_limit_stops_remaining_reads(data, code):
    market = Market(data)
    market.fail['005930'] = TossApiError(code, 'fixture')
    result = screen(data, market)
    assert result['eligibleCount'] == 0
    assert ('get_candles', '000660') not in market.calls


def test_index_failure_cancels_all_candidates(data):
    market = Market(data)
    market.fail['KOSPI'] = ValueError('missing index')
    result = screen(data, market)
    assert result['status'] == 'failed' and result['eligibleCount'] == 0
    assert not any(symbol in {'005930', '000660'} for _, symbol in market.calls)


def test_deadline_invalidates_previously_collected_rows(data):
    market = Market(data)
    call = market.call
    def slow(*args, **kwargs):
        rows = call(*args, **kwargs)
        if args[1] == '000660': market.now += timedelta(minutes=4)
        return rows
    market.call = slow
    result = screen(data, market)
    assert result['status'] == 'failed' and result['eligibleCount'] == 0


@pytest.mark.parametrize('reason', ['historical', 'closed', 'too-early'])
def test_outside_session_or_insufficient_market_data_blocks_all(data, reason):
    market = Market(data)
    if reason == 'historical': market.now += timedelta(days=1)
    if reason == 'closed': market.now = market.now.replace(hour=15, minute=1)
    if reason == 'too-early': market.now = market.now.replace(minute=10)
    result = screen(data, market)
    assert result['status'] == 'failed' and not result['eligibleCount']


def index_close(market, price):
    call = market.call
    def changed(method, symbol, **kwargs):
        result = call(method, symbol, **kwargs)
        if symbol == 'KOSPI':
            result['candles'][-1].update(closePrice=str(price), lowPrice=str(min(price, 9990)),
                                         highPrice=str(max(price, 10010)))
        return result
    market.call = changed


@pytest.mark.parametrize('price,blocked', [(9700, True), (9900, True), (9901, False), (10100, False)])
def test_market_drop_is_normal_veto_with_exact_boundary(data, price, blocked):
    market = Market(data)
    index_close(market, price)
    result = screen(data, market)
    assert result['marketGate']['status'] == ('blocked' if blocked else 'passed')
    assert result['marketReturn'] == pytest.approx(price / 10000 - 1)
    assert result['error'] is None
    if blocked:
        assert result['status'] == 'market-blocked' and result['eligibleCount'] == 0
        assert result['marketBlockedCount'] == 2 and result['unavailableCount'] == 0
        assert not any(symbol in {'005930', '000660'} for _, symbol in market.calls)
        assert all(not row['intraday']['eligible'] and not row['intraday']['watchable'] for row in result['rows'])
        assert all(row['intraday']['assessment'] == 'market-blocked' for row in result['rows'])
        assert '-1.00%' in result['notice'] and '전일 종가 대비 아님' in result['notice']
    else:
        assert result['status'] == 'completed'


@pytest.mark.parametrize('value', [float('nan'), float('inf'), -float('inf')])
def test_invalid_index_still_fails_and_does_not_serialize_nonfinite_data(data, monkeypatch, value):
    from tradingagents.brokers import candidate_screen
    monkeypatch.setattr(candidate_screen, 'market_reference', lambda *a: {'return': value})
    result = screen(data)
    assert result['status'] == 'failed' and result['marketGate']['status'] == 'unavailable'
    assert '지수 등락률이 유효하지 않습니다' in result['error']
    assert 'marketReference' not in result and 'marketReturn' not in result


@pytest.mark.parametrize('limit', ['0', '-.01', 'nan', 'inf'])
def test_invalid_market_limit_cannot_silently_disable_veto(data, limit):
    market = Market(data)
    result = screen_candidates(candidates(), market, {'market_drop_limit': limit, 'buy_start_minutes': 20},
                               requested_date=data[4].date().isoformat(), clock=lambda: market.now)
    assert result['status'] == 'failed' and result['marketGate']['status'] == 'unavailable'


def test_market_veto_is_not_latched_after_recovery(data):
    market = Market(data)
    original = market.call
    index_close(market, 9700)
    assert screen(data, market)['status'] == 'market-blocked'
    market.call = original
    result = screen(data, market)
    assert result['status'] == 'completed' and result['eligibleCount'] == 2


def test_market_veto_preserves_observed_opening_gap_basis(data):
    market = Market(data)
    index_close(market, 9700)
    call = market.call
    def missing_first(method, symbol, **kwargs):
        result = call(method, symbol, **kwargs)
        if symbol == 'KOSPI':
            result['candles'] = result['candles'][1:]
        return result
    market.call = missing_first
    result = screen(data, market)
    assert result['status'] == 'market-blocked'
    assert result['marketGate']['openingGap'] is True
    assert result['marketGate']['basisStart'] == (data[3] + timedelta(minutes=1)).isoformat()
    assert '09:01:00' in result['notice'] and result['warnings']


@pytest.mark.parametrize('live', [False, True])
@pytest.mark.parametrize('engine', ['rules-news', 'multi-agent'])
def test_market_blocked_auto_run_completes_without_llm_orders_or_account_screen(data, tmp_path, monkeypatch, live, engine):
    monkeypatch.setattr(main, 'LOCAL_DIR', tmp_path)
    monkeypatch.setattr(main, 'is_regular_market_open', lambda *args, **kwargs: True)
    monkeypatch.setattr(main, 'collect_candidate_snapshot', lambda args: {
        'ok': True, 'universe': universe_snapshot(), 'candidates': candidates()})
    market = Market(data)
    index_close(market, 9700)
    manager = object.__new__(main.PositionRiskManager)
    manager.backend = market
    manager.candidate_exclusions = Mock(side_effect=AssertionError('No account reads during market veto'))
    manager.execute = Mock(side_effect=AssertionError('No orders during market veto'))
    manager.stop = Mock(side_effect=AssertionError('Do not stop holdings monitoring'))
    monkeypatch.setattr(main, 'screen_candidates', lambda *args, **kwargs:
                        screen_candidates(*args, **kwargs, clock=lambda: market.now))
    analyze = AsyncMock(side_effect=AssertionError('No LLM analysis during market veto'))
    monkeypatch.setattr(main, 'run_korea_toss', analyze)
    args = main.make_parser().parse_args(['--run-mode', 'once', '--yes', '--intraday-engine', engine, '--buy-start-minutes', '20',
                                         '--date', data[4].date().isoformat()])
    args._position_manager = manager
    result = asyncio.run(main.run_auto_trading(args, execute=live))
    assert result['ok'] and result['selectedTickers'] == [] and result['analyses'] == []
    assert result['marketGate']['status'] == 'blocked' and result['marketGate']['threshold'] == -.01
    assert '시장 위험으로 신규 매수 보류' in result['selectionReason']
    assert args.market_drop_limit == '0.01'
    analyze.assert_not_called()
    manager.candidate_exclusions.assert_not_called()
    manager.execute.assert_not_called()
    manager.stop.assert_not_called()


def test_market_blocked_snapshot_and_dashboard_keep_explanation(data, tmp_path, monkeypatch):
    import json
    path, markdown = tmp_path / 'candidates.json', tmp_path / 'candidates.md'
    snapshot = {'ok': True, 'candidates': candidates(), 'jsonPath': str(path), 'markdownPath': str(markdown)}
    path.write_text(json.dumps(snapshot))
    markdown.write_text('# News\n')
    market = Market(data)
    index_close(market, 9700)
    monkeypatch.setattr(main, 'BrokerBackend', lambda *args: market)
    monkeypatch.setattr(main, 'screen_candidates', lambda *args, **kwargs:
                        screen_candidates(*args, **kwargs, clock=lambda: market.now))
    args = main.make_parser().parse_args(['--date', data[4].date().isoformat(), '--buy-start-minutes', '20'])
    result = main.screen_candidate_snapshot(args, snapshot)
    saved = json.loads(path.read_text())
    assert saved['ok'] and saved['intradayScreen']['status'] == 'market-blocked'
    assert saved['intradayScreen']['marketGate']['basisStart'] == data[3].isoformat()
    assert '-3.00%' in saved['selectionReason'] and '-1.00%' in saved['selectionReason']
    assert '-3.00%' in markdown.read_text()
    html = main.render_monitor_html({'latestCandidates': saved})
    assert '시장평가: 시장 위험으로 신규 매수 보류' in html
    assert '-3.00%' in html and '-1.00%' in html and '전일 종가 대비 아님' in html


def test_user_stop_is_propagated(data, monkeypatch):
    from tradingagents.brokers import candidate_screen
    def stop(*args): raise RunStopped('stop')
    monkeypatch.setattr(candidate_screen, 'checkpoint', stop)
    with pytest.raises(RunStopped): screen(data)


@pytest.mark.parametrize('live', [False, True])
def test_auto_pipeline_uses_reranked_shortlist_and_same_llm_limit(data, tmp_path, monkeypatch, live):
    monkeypatch.setattr(main, 'LOCAL_DIR', tmp_path)
    monkeypatch.setattr(main, 'is_regular_market_open', lambda *args, **kwargs: True)
    monkeypatch.setattr(main, 'collect_candidate_snapshot', lambda args: {
        'ok': True, 'universe': universe_snapshot(), 'candidates': candidates()})
    market = Market(data)
    monkeypatch.setattr(main, 'BrokerBackend', lambda *args: market)
    monkeypatch.setattr(main, 'screen_candidates', lambda *args, **kwargs: screen_candidates(*args, **kwargs, clock=lambda: market.now))
    analyze = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(main, 'run_korea_toss', analyze)
    args = main.make_parser().parse_args(['--run-mode', 'once', '--yes', '--trade-candidates', '1', '--buy-start-minutes', '20',
        '--date', data[4].date().isoformat(), '--min-session-turnover', '1000000'])
    result = asyncio.run(main.run_auto_trading(args, execute=live))
    assert result['selectedTickers'] == ['000660.KS'] and analyze.call_count == 1
    assert result['candidateDiscovery']['intradayScreen']['eligibleCount'] == 2
    market.fail['KOSPI'] = ValueError('unavailable')
    analyze.reset_mock()
    result = asyncio.run(main.run_auto_trading(args, execute=live))
    assert not result['ok'] and not result['selectedTickers'] and result['selectionReason']
    analyze.assert_not_called()


def test_candidate_ui_retains_news_and_explains_market_exclusion(data):
    result = screen(data)
    result['rows'][0]['intraday'].update(eligible=False, reasons=['<script>unsafe</script>'])
    html = main.render_monitor_html({'latestCandidates': {'candidates': result['rows']}})
    section = html.split('<section data-live="candidates">')[1].split('</section>')[0]
    assert '뉴스 점수' in section and '단타 점수' in section and '시장 대비' in section
    assert '&lt;script&gt;' in section and '<script>' not in section


@pytest.mark.parametrize('settings', [{'market_open': '10:00'}, {'market_close': '09:40'},
                                    {'holiday_dates': '2026-09-15'}])
def test_user_hours_and_holidays_are_respected(data, settings):
    market = Market(data)
    result = screen_candidates(candidates(), market, settings,
        requested_date=data[4].date().isoformat(), clock=lambda: market.now)
    assert result['status'] == 'failed' and not result['eligibleCount']
    assert not any(name == 'get_candles' for name, _ in market.calls)


def test_screen_snapshot_preserves_news_and_saves_full_market_evidence(data, tmp_path, monkeypatch):
    import json
    path, markdown = tmp_path / 'candidates.json', tmp_path / 'candidates.md'
    snapshot = {'ok': True, 'candidates': candidates(), 'jsonPath': str(path), 'markdownPath': str(markdown)}
    path.write_text(json.dumps({**snapshot, 'articles': [{'title': 'original'}]}))
    markdown.write_text('# News Ranking\n')
    market = Market(data)
    monkeypatch.setattr(main, 'BrokerBackend', lambda *args: market)
    monkeypatch.setattr(main, 'screen_candidates', lambda *args, **kwargs: screen_candidates(*args, **kwargs, clock=lambda: market.now))
    args = main.make_parser().parse_args(['--date', data[4].date().isoformat(), '--min-session-turnover', '1000000', '--buy-start-minutes', '20'])
    result = main.screen_candidate_snapshot(args, snapshot)
    saved = json.loads(path.read_text())
    assert saved['articles'] == [{'title': 'original'}]
    assert saved['candidates'] == result['candidates']
    assert saved['candidates'][0]['intraday']['components']
    assert saved['intradayScreen']['eligibleCount'] == 2
    assert markdown.read_text().startswith('# News Ranking')
    assert 'Intraday Market Ranking' in markdown.read_text()


def test_zero_analysis_count_does_not_fetch_market_data(tmp_path, monkeypatch):
    from unittest.mock import Mock
    monkeypatch.setattr(main, 'LOCAL_DIR', tmp_path)
    monkeypatch.setattr(main, 'collect_candidate_snapshot', lambda args: {
        'ok': True, 'universe': universe_snapshot(), 'candidates': candidates()})
    screen = Mock(side_effect=AssertionError('no market reads'))
    monkeypatch.setattr(main, 'screen_candidate_snapshot', screen)
    args = main.make_parser().parse_args(['--trade-candidates', '0', '--run-mode', 'once'])
    result = asyncio.run(main.run_auto_trading(args, execute=False))
    assert result['selectedTickers'] == []
    screen.assert_not_called()


def test_rate_limited_candidates_are_not_reported_as_strategy_rejections(data):
    market = Market(data)
    error = TossApiError(429, 'synthetic limit')
    error.rate_limit = {'retryAt': '2026-09-15T00:21:00+00:00', 'attempts': 3, 'group': 'MARKET_DATA_CHART'}
    market.fail['000660'] = error
    result = screen(data, market)
    assert result['evaluatedCount'] == 1 and result['unavailableCount'] == 1
    assert result['eligibleCount'] == 1 and result['status'] == 'partial'
    assert '미평가' in result['notice'] and error.rate_limit['retryAt'] in result['notice']
    assert result['rows'][1]['intraday']['reasonCode'] == 'rate-limited'
    assert result['rows'][1]['intraday']['rateLimit']['attempts'] == 3
    market.fail['005930'] = error
    result = screen(data, market)
    assert result['evaluatedCount'] == 0 and result['unavailableCount'] == 2
    assert result['rows'][1]['intraday']['assessment'] == 'deferred'


def test_index_rate_limit_has_retry_time_and_zero_evaluated_candidates(data):
    market = Market(data)
    error = TossApiError(429, 'synthetic limit')
    error.rate_limit = {'retryAt': '2026-09-15T00:21:00+00:00'}
    market.fail['KOSPI'] = error
    result = screen(data, market)
    assert result['status'] == 'failed' and result['evaluatedCount'] == 0
    assert result['unavailableCount'] == 2 and '미평가' in result['error']
    assert error.rate_limit['retryAt'] in result['error']


def test_rate_limit_notice_is_saved_instead_of_no_eligible_candidates(data, tmp_path, monkeypatch):
    import json
    path = tmp_path / 'candidates.json'
    snapshot = {'ok': True, 'candidates': candidates(), 'jsonPath': str(path)}
    path.write_text(json.dumps(snapshot))
    market = Market(data)
    error = TossApiError(429, 'synthetic limit')
    error.rate_limit = {'retryAt': '2026-09-15T00:21:00+00:00'}
    market.fail['005930'] = error
    monkeypatch.setattr(main, 'BrokerBackend', lambda *args: market)
    monkeypatch.setattr(main, 'screen_candidates', lambda *args, **kwargs:
                        screen_candidates(*args, **kwargs, clock=lambda: market.now))
    args = main.make_parser().parse_args(['--date', data[4].date().isoformat(), '--buy-start-minutes', '20'])
    result = main.screen_candidate_snapshot(args, snapshot)
    saved = json.loads(path.read_text())
    assert saved['selectionReason'] == result['selectionReason']
    assert '미평가' in result['selectionReason'] and '조건을 통과한 후보 없음' not in result['selectionReason']
    html = main.render_monitor_html({'latestCandidates': result})
    assert '시장자료 평가 0/2종목' in html and '재시도 가능 시각' in html

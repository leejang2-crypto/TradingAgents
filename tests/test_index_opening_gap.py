from copy import deepcopy
from datetime import timedelta
from unittest.mock import Mock
import json

import pytest

import main
from tests.test_intraday import data, check
from tests.test_candidate_screen import Market, candidates, screen
from tradingagents.brokers.intraday import (
    CandleDataError, bars, market_reference, session_candles, stamp,
)
from tradingagents.brokers.candidate_screen import measure_candidate, screen_candidates


@pytest.mark.parametrize('symbol', ['KOSPI', 'KOSDAQ'])
@pytest.mark.parametrize('minutes', [20, 335])
def test_only_first_index_minute_is_accepted_without_fabricating_data(data, symbol, minutes):
    start = data[3]
    now = start + timedelta(minutes=minutes, seconds=5)
    raw = [{**data[0][0], 'timestamp': (start + timedelta(minutes=i)).isoformat()}
           for i in range(1, minutes)]
    original = deepcopy(raw)
    call = Mock(return_value={'candles': raw, 'nextBefore': None})
    info = {}
    rows = session_candles(call, symbol, start, now, index=True, diagnostics=info)
    assert len(rows) == minutes - 1 and raw == original
    assert info['code'] == 'opening-gap' and info['acceptedOpeningGap']
    assert info['missingAt'] == [(start + timedelta(minutes=1)).isoformat()]
    assert info['expectedCount'] == minutes and info['receivedCount'] == minutes - 1
    assert info['retryCount'] == 1 and call.call_count == 2 and info['warning']
    assert stamp(info['firstAt']) == start + timedelta(minutes=2)
    reference = market_reference(rows, start, now)
    assert reference['basisStart'] == (start + timedelta(minutes=1)).isoformat()
    assert reference['barEnd'] == (start + timedelta(minutes=minutes)).isoformat()
    assert reference['openingGap'] and reference['warning'] and reference['return'] == 0
    five, _, _ = bars(rows, start, now, index=True)
    assert len(five) == minutes // 5 - 1
    assert five.index[0] == start + timedelta(minutes=10)


@pytest.mark.parametrize('removed', [[0, 1], [4], [19], [0, 4], [0, 19]])
def test_other_index_gaps_remain_blocked(data, monkeypatch, removed):
    monkeypatch.setattr('tradingagents.brokers.intraday.time.sleep', lambda _: None)
    rows, _, _, start, now = data
    rows = [row for i, row in enumerate(rows) if i not in removed]
    raw = [{**row, 'timestamp': (stamp(row['timestamp']) - timedelta(minutes=1)).isoformat()} for row in rows]
    with pytest.raises(CandleDataError):
        session_candles(Mock(return_value={'candles': raw}), 'KOSPI', start, now, index=True)
    with pytest.raises(CandleDataError):
        market_reference(rows, start, now)


def test_opening_exception_does_not_hide_unaligned_or_invalid_prices(data):
    rows, _, _, start, now = deepcopy(data)
    rows = rows[1:]
    rows[3]['timestamp'] = (stamp(rows[3]['timestamp']) + timedelta(seconds=1)).isoformat()
    with pytest.raises(CandleDataError) as error:
        market_reference(rows, start, now)
    assert error.value.diagnostics['code'] == 'unaligned'
    rows[3]['timestamp'] = data[0][4]['timestamp']
    rows[3]['openPrice'] = 'NaN'
    with pytest.raises(ValueError):
        market_reference(rows, start, now)


@pytest.mark.parametrize('symbol,index', [('005930', False), ('OTHER', True), ('KOSPI', False)])
def test_exception_is_not_available_to_stocks_or_unknown_indices(data, symbol, index):
    rows, _, _, start, now = data
    offset = timedelta(minutes=1) if index else timedelta(0)
    raw = [{**row, 'timestamp': (stamp(row['timestamp']) - offset).isoformat()} for row in rows[1:]]
    with pytest.raises(CandleDataError):
        session_candles(Mock(return_value={'candles': raw}), symbol, start, now, index=index)
    with pytest.raises(CandleDataError):
        bars(rows[1:], start, now)


def test_no_exception_before_twenty_minutes_and_complete_index_is_unchanged(data):
    rows, _, _, start, now = data
    with pytest.raises(CandleDataError):
        market_reference(rows[1:], start, start + timedelta(minutes=19))
    reference = market_reference(rows, start, now)
    assert reference['basisStart'] == start.isoformat()
    assert not reference['openingGap'] and reference['warning'] is None
    assert reference['return'] == pytest.approx(10060 / 10000 - 1)
    assert len(bars(rows, start, now, index=True)[0]) == 4


def test_relative_strength_uses_same_minute_as_index_not_session_open(data):
    rows, _, _, start, now = deepcopy(data)
    rows[0].update(openPrice='9900', lowPrice='9900')
    rows[1].update(openPrice='10010')
    market_return = .0055
    aligned = measure_candidate(rows, start, now, market_return, {'min_session_turnover': '1000000'},
                                reference_start=start + timedelta(minutes=1))
    full = measure_candidate(rows, start, now, market_return, {'min_session_turnover': '1000000'})
    metrics = aligned['metrics']
    assert metrics['sessionReturn'] == pytest.approx(10060 / 9900 - 1)
    assert metrics['comparisonReturn'] == pytest.approx(10060 / 10010 - 1)
    assert metrics['relativeStrength'] == pytest.approx(10060 / 10010 - 1 - market_return)
    assert not aligned['eligible'] and full['eligible']
    assert full['score'] > aligned['score']
    with pytest.raises(ValueError, match='기준 시각'):
        measure_candidate(rows, start, now, 0, {}, reference_start=start + timedelta(minutes=2))


@pytest.mark.parametrize('falling', [False, True])
def test_final_entry_applies_exception_without_bypassing_market_drop_guard(data, falling):
    data = deepcopy(data)
    for rows in data[2].values():
        rows.pop(0)
        if falling:
            for row in rows[-5:]:
                row.update(closePrice='9700', lowPrice='9600')
    result = check(data)
    assert result['allowed'] is not falling
    assert len(result['warnings']) == 2
    assert all(ref['openingGap'] for ref in result['marketReferences'].values())
    if falling:
        assert '시장 지수 하락 제한' in result['reasons']
    data[0].pop(0)
    with pytest.raises(CandleDataError):
        check(data)


def missing_opening_market(data):
    market = Market(data)
    call = market.call
    def missing(*args, **kwargs):
        result = call(*args, **kwargs)
        if args[1] == 'KOSPI':
            result['candles'] = result['candles'][1:]
        return result
    market.call = missing
    return market


def test_candidate_pipeline_accepts_index_gap_and_aligns_all_candidates(data):
    result = screen(data, missing_opening_market(data))
    assert result['status'] == 'completed' and result['eligibleCount'] == 2
    assert result['marketData']['missingCount'] == 1 and result['warnings']
    assert all(row['intraday']['metrics']['comparisonStart'] == result['marketReference']['basisStart']
               for row in result['rows'])


def test_warning_and_basis_are_saved_and_visible_without_losing_existing_news(data, tmp_path, monkeypatch):
    path, markdown = tmp_path / 'candidates.json', tmp_path / 'candidates.md'
    snapshot = {'ok': True, 'candidates': candidates(), 'warnings': ['original news warning'],
                'jsonPath': str(path), 'markdownPath': str(markdown)}
    path.write_text(json.dumps({**snapshot, 'articles': [{'title': 'original'}]}))
    markdown.write_text('# News Ranking\n')
    market = missing_opening_market(data)
    monkeypatch.setattr(main, 'BrokerBackend', lambda *args: market)
    monkeypatch.setattr(main, 'screen_candidates', lambda *args, **kwargs:
                        screen_candidates(*args, **kwargs, clock=lambda: market.now))
    args = main.make_parser().parse_args(['--date', data[4].date().isoformat(), '--min-session-turnover', '1000000'])
    result = main.screen_candidate_snapshot(args, snapshot)
    saved = json.loads(path.read_text())
    assert saved['warnings'] == result['warnings'] and len(saved['warnings']) == 2
    assert saved['warnings'][0] == 'original news warning'
    assert saved['articles'] == [{'title': 'original'}]
    assert '첫 1분 변동 미반영' in markdown.read_text()
    basis = saved['intradayScreen']['marketReference']['basisStart']
    assert basis in markdown.read_text()
    result['warnings'].append('<script>unsafe</script>')
    html = main.render_monitor_html({'latestCandidates': result})
    section = html.split('<section data-live="candidates">')[1].split('</section>')[0]
    assert '첫 1분 변동 미반영' in section and '비교 기준' in section and basis in section
    assert '&lt;script&gt;' in section and '<script>' not in section

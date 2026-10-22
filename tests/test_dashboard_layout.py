from collections import Counter
from html.parser import HTMLParser

import main


class Elements(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def test_layout_preserves_existing_controls_and_unique_ids():
    parsed = Elements(main.render_monitor_html({}))
    ids = Counter(attrs['id'] for _, attrs in parsed.tags if 'id' in attrs)
    assert all(count == 1 for count in ids.values())
    required = {
        'auto-run-panel', 'auto-run-once', 'auto-run-mode', 'auto-run-state', 'auto-run-message',
        'manual-panel', 'manual-ticker', 'auto-paused', 'manual-mode', 'current-job',
        'pending-job', 'last-job', 'cancel-pending', 'manual-result', 'manual-result-text',
        'control-message', 'run-dialog', 'run-stop', 'run-wait', 'run-confirm', 'run-dismiss',
        'positions-panel', 'positions-status', 'positions-daily', 'positions-body', 'entry-checks',
        'positions-warning', 'position-adoption', 'position-adopt-form', 'position-adopt-message',
        'position-orders', 'account-panel', 'account-date', 'account-load', 'account-refresh',
        'account-message', 'account-results', 'account-source', 'account-metrics', 'account-note',
        'returns-title', 'account-returns', 'account-holdings', 'account-orders', 'logout',
        'settings-message', 'ollama-models', 'openai-models', 'ollama-status', 'agent-routing',
    }
    assert required <= set(ids)
    assert {attrs['data-action'] for _, attrs in parsed.tags if 'data-action' in attrs} == {'news', 'candidates', 'analyze', 'trade'}
    fields = {attrs['name'] for _, attrs in parsed.tags if 'name' in attrs}
    settings = set(main.default_settings(main.make_parser().parse_args([]))) - {'daily_target_return', 'hybrid_agent_providers'}
    assert settings <= fields
    assert {'route_' + role for role in main.AGENT_LABELS} <= fields
    periods = [attrs['value'] for _, attrs in parsed.tags if attrs.get('name') == 'return_period']
    assert periods == ['day', 'week', 'month', 'year']


def test_auto_run_button_starts_disabled_and_uses_existing_trade_action():
    parsed = Elements(main.render_monitor_html({}))
    button = next(attrs for tag, attrs in parsed.tags if attrs.get('id') == 'auto-run-once')
    assert button['type'] == 'button' and button['data-action'] == 'trade'
    assert 'disabled' in button
    assert '자동실행 (1회)' in main.render_monitor_html({})


def test_tabs_details_and_new_assets_are_declared():
    parsed = Elements(main.render_monitor_html({}))
    tabs = [attrs for _, attrs in parsed.tags if attrs.get('role') == 'tab']
    assert [tab['data-view'] for tab in tabs] == ['overview', 'analysis', 'research', 'history', 'execution', 'settings']
    assert all(tab['aria-controls'] == 'view-' + tab['data-view'] for tab in tabs)
    groups = [attrs for tag, attrs in parsed.tags if tag == 'details' and attrs.get('class') == 'settings-group']
    assert len(groups) == 5
    assert sum('open' in group for group in groups) == 1
    assert any(attrs.get('src') == '/assets/dashboard.js' for _, attrs in parsed.tags)
    assert any(attrs.get('href') == '/assets/dashboard.css' for _, attrs in parsed.tags)


def test_analysis_full_text_and_existing_fields_remain_escaped():
    decision = 'Detailed evidence ' * 50 + '<script>bad()</script>'
    html = main.render_monitor_html({
        'latestSummaryPath': '/private/summary.json',
        'latestCandidatePath': '/private/candidates.json',
        'latestSummary': {'analyses': [{'ticker': '005930', 'name': '삼성전자', 'candidateRank': 1,
                                      'decision': decision, 'reportPath': '/private/report.md',
                                      'provider': 'hybrid', 'agentRouting': {'news_analyst': {'provider': 'ollama', 'model': 'qwen3:8b'}}}]},
    })
    assert main.html.escape(decision) in html
    assert '<script>bad()</script>' not in html
    for value in ('005930', '삼성전자', '/private/report.md', '/private/summary.json', '/private/candidates.json', 'qwen3:8b'):
        assert value in html
    assert '<th>Ticker</th><th>종목명</th><th>Rank</th><th>Decision / Error</th><th>Order</th><th>Report</th><th>사용 LLM</th>' in html


def test_summary_path_opens_accessible_in_dashboard_viewer():
    html = main.render_monitor_html({
        'executionMode': 'live', 'latestSummaryPath': '/private/auto_live/auto_live_2026-09-16.json',
    })
    parsed = Elements(html)
    links = [attrs for tag, attrs in parsed.tags if tag == 'a' and 'data-summary-link' in attrs]
    assert len(links) == 1
    assert links[0]['href'] == '/api/analysis-summary?mode=live&file=auto_live_2026-09-16.json'
    assert links[0]['aria-haspopup'] == 'dialog'
    assert any(tag == 'dialog' and attrs.get('id') == 'summary-dialog' for tag, attrs in parsed.tags)
    assert 'data-summary-link' not in main.render_monitor_html({})
    panel = next(attrs for _, attrs in parsed.tags if attrs.get('id') == 'local-summary-panel')
    assert panel['data-query'] == 'mode=live&file=auto_live_2026-09-16.json'
    assert html.index('analysis-path') < html.index('local-summary-panel')

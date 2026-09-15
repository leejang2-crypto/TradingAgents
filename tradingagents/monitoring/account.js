/* Account data stays in memory; credentials are never put in URLs/storage. */
(() => {
  let loaded = false;
  let sequence = 0;
  const el = id => document.getElementById(id);
  const fmt = (value, suffix = '') => value === null || value === undefined || value === ''
    ? '-' : Number.isFinite(Number(value)) ? Number(value).toLocaleString('ko-KR', {maximumFractionDigits: 2}) + suffix : String(value);
  function cell(tag, text) { const node = document.createElement(tag); node.textContent = text ?? '-'; return node; }
  function table(target, headers, rows, empty) {
    const container = el(target); container.replaceChildren();
    if (!rows.length) { container.append(cell('p', empty)); return; }
    const table = document.createElement('table');
    const head = document.createElement('thead'), tr = document.createElement('tr');
    headers.forEach(h => tr.append(cell('th', h))); head.append(tr); table.append(head);
    const body = document.createElement('tbody');
    rows.forEach(values => { const row = document.createElement('tr'); values.forEach(v => row.append(cell('td', v))); body.append(row); });
    table.append(body); container.append(table);
  }
  function metrics(values) {
    el('account-metrics').replaceChildren(...values.map(([label, value]) => {
      const item = document.createElement('div'); item.className = 'metric';
      const title = cell('div', label), content = cell('div', value); title.className = 'label'; content.className = 'value';
      item.append(title, content); return item;
    }));
  }
  function render(data) {
    const account = data.account;
    el('account-results').hidden = false;
    if (data.mode === 'dry-run') {
      el('account-source').textContent = '가상 체결 계좌 · KRW · ' + (account.asOf || '-');
      metrics([['현금 잔고', fmt(account.cash, ' 원')], ['총 평가자산', fmt(account.equity, ' 원')],
        ['주식 평가금액', fmt(account.marketValue, ' 원')], ['누적 실현손익', fmt(account.realizedPnl, ' 원')], ['미실현손익', fmt(account.unrealizedPnl, ' 원')]]);
      const costs = account.assumptions || {};
      el('account-note').textContent = '수수료 ' + fmt(Number(costs.feeRate || 0) * 100, '%') + ' · 매도세율 ' + fmt(Number(costs.sellTaxRate || 0) * 100, '%')
        + ' · 슬리피지 ' + fmt(Number(costs.slippageRate || 0) * 100, '%') + (account.valuationComplete ? '' : ' · 최신 시세 미확인: 평가액·수익률 산출 보류');
      el('returns-title').textContent = '기간별 수익률 · 모의계좌';
      table('account-returns', ['기간 시작', '기준 평가액', '최종 평가액', '손익', '수익률', '기간 실현손익', '평가 시점'],
        data.returns.map(r => [r.period, fmt(r.startEquity), fmt(r.endEquity), fmt(r.pnl), fmt(r.returnPct, '%'), fmt(r.realizedPnl),
          r.startAt + ' → ' + r.endAt + (r.partial ? ' · 부분 기간' : ' · 관측 기준')]), '해당 날짜까지의 평가 이력이 없습니다.');
      table('account-holdings', ['종목', '보유 수량', '평균 매입원가', '현재가', '평가금액', '미실현손익', '시세 시각'],
        (account.positions || []).map(p => [p.symbol, fmt(p.quantity), fmt(p.averagePrice), fmt(p.lastPrice), fmt(p.marketValue), fmt(p.unrealizedPnl),
          (p.quoteTime || '-') + (p.stale ? ' · 지연' : '')]), '보유주식 없음');
      table('account-orders', ['시각', '종목', '매매', '수량', '체결가', '상태 / 사유'],
        (account.trades || []).map(t => [t.recordedAt, t.symbol, t.side, fmt(t.quantity), fmt(t.price), t.status + (t.reason ? ' · ' + t.reason : '')]), '가상 체결 이력 없음');
    } else {
      const h = account?.holdings, power = account?.buyingPower || {};
      el('account-source').textContent = 'Toss API · ' + (account?.asOf || '미조회');
      metrics([['KRW 매수 가능 금액', fmt(power.KRW?.cashBuyingPower, ' 원')], ['USD 매수 가능 금액', fmt(power.USD?.cashBuyingPower, ' USD')],
        ['국내 주식 평가액', fmt(h?.marketValue?.amount?.krw, ' 원')], ['해외 주식 평가액', fmt(h?.marketValue?.amount?.usd, ' USD')],
        ['보유주식 평가수익률', h?.profitLoss?.rate == null ? '-' : fmt(Number(h.profitLoss.rate) * 100, '%')],
        ['보유주식 당일 변동률', h?.dailyProfitLoss?.rate == null ? '-' : fmt(Number(h.dailyProfitLoss.rate) * 100, '%')]]);
      const failures = Object.entries(account?.errors || {}).map(([key, value]) => key + ': ' + value).join(', ');
      el('account-note').textContent = '매수 가능 금액은 현금 잔고가 아닙니다. 보유주식 수익률은 계좌 전체 실현수익률이 아닙니다.' + (failures ? ' · 조회 오류: ' + failures : '');
      el('returns-title').textContent = '기간별 계좌 수익률 · Toss';
      table('account-returns', [], [], data.returnAvailability);
      table('account-holdings', ['종목', '이름', '통화', '수량', '평균 매입가', '현재가', '평가금액', '평가손익'],
        (h?.items || []).map(p => [p.symbol, p.name, p.currency, fmt(p.quantity), fmt(p.averagePurchasePrice), fmt(p.lastPrice), fmt(p.marketValue?.amount), fmt(p.profitLoss?.amount)]), h ? '보유주식 없음' : '보유주식 조회 전 또는 조회 실패');
      table('account-orders', ['시각', '종목', '매매', '통화', '체결 수량', '평균 체결가', '상태'],
        (account?.orders || []).map(o => [o.orderedAt, o.symbol, o.side, o.currency, fmt(o.execution?.filledQuantity), fmt(o.execution?.averageFilledPrice), o.status]), '최근 주문 조회 전 또는 내역 없음');
      if (account?.ordersTruncated) el('account-orders').append(cell('p', '최근 30일 주문 중 첫 페이지입니다. 추가 주문이 있습니다.'));
    }
    document.dispatchEvent(new Event('dashboard-data'));
  }
  async function load(refresh = false) {
    const mode = document.querySelector('[name="account_mode"]:checked').value;
    const period = document.querySelector('[name="return_period"]:checked').value;
    const id = ++sequence;
    el('account-message').textContent = refresh ? '최신 자료 조회 중...' : '조회 중...';
    const headers = {'X-Requested-With': 'TradingAgents', 'Content-Type': 'application/json'};
    try {
      if (refresh) {
        const response = await fetch('/api/account/refresh', {method: 'POST', headers, body: JSON.stringify({mode})});
        const result = await response.json(); if (!response.ok) throw new Error(result.error);
      }
      const params = new URLSearchParams({mode, period}); if (el('account-date').value) params.set('through', el('account-date').value);
      const response = await fetch('/api/account?' + params, {headers, cache: 'no-store'});
      const data = await response.json(); if (!response.ok) throw new Error(data.error);
      if (id !== sequence) return;
      loaded = true; render(data); el('account-message').textContent = '조회 완료';
    } catch (error) { if (id === sequence) el('account-message').textContent = error.message || '조회 실패'; }
  }
  document.addEventListener('DOMContentLoaded', () => {
    el('account-load').addEventListener('click', () => load());
    el('account-refresh').addEventListener('click', () => load(true));
    document.querySelectorAll('[name="account_mode"],[name="return_period"],#account-date').forEach(input => input.addEventListener('change', () => {
      sequence++; el('account-results').hidden = true; document.dispatchEvent(new Event('dashboard-data')); if (loaded) load();
    }));
    setInterval(() => { if (loaded) load(); }, 30000);
  });
})();

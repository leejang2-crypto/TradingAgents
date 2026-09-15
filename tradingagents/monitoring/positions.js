(() => {
  const el = id => document.getElementById(id);
  const text = (tag, value) => { const node = document.createElement(tag); node.textContent = value ?? '-'; return node; };
  let editing = false;
  async function refresh() {
    if (editing) return;
    try {
      const response = await fetch('/api/positions', {cache: 'no-store'});
      if (response.status === 401) { location.assign('/login'); return; }
      if (!response.ok) throw new Error('보유 관리 조회 실패');
      const data = await response.json();
      if (!data.enabled) { el('positions-status').textContent = '보유 관리 실행기 없음'; return; }
      el('position-adoption').hidden = data.mode !== 'live';
      el('positions-status').textContent = (data.mode === 'live' ? '실투자' : '모의투자') + ' · ' + (data.checkedAt || '첫 점검 대기') + ' · ' + (data.marketOpen ? '정규장 점검 중' : '장외 · 체결 확인 중');
      const daily = data.daily?.pnl != null ? data.daily : null;
      el('positions-daily').textContent = '종목별 익절 +' + (Number(data.goalReturn) * 100).toFixed(2) + '% (비용 전) · 일일 손실 한도 ' + (Number(data.dailyLossLimit) * 100).toFixed(2) + '% · ' + (daily ? '관리 전략 손익: ' + Number(daily.pnl).toLocaleString('ko-KR') + '원 · 자본 대비 ' + Number(daily.returnPct).toFixed(2) + '% · ' + (daily.lossLimitReached ? '손실 한도 도달 · 청산 및 매수 중단' : daily.stopLimitReached ? '연속 손절 한도 · 매수 중단' : '손실 한도 내') + (daily.partial ? ' · 관측 시작 기준 손익' : '') : '당일 손익 기준 수집 전');
      const rows = Object.values(data.positions || {}).filter(p => Number(p.quantity) > 0).map(p => {
        const row = document.createElement('tr');
        const price = value => value == null ? '-' : Number(value).toLocaleString('ko-KR', {maximumFractionDigits: 2});
        [p.symbol, p.quantity, price(p.averagePrice), p.returnPct == null ? '-' : Number(p.returnPct).toFixed(2) + '%', price(p.targetPrice) + ' / ' + price(p.stopPrice), p.acquiredDate, p.maxHoldingDate || '거래일 확인 중'].forEach(v => row.append(text('td', v)));
        const cell = document.createElement('td');
        const input = document.createElement('input'); input.type = 'checkbox'; input.checked = p.carryApproved;
        input.setAttribute('aria-label', p.symbol + ' 이월 허용'); input.style.width = '20px'; input.style.height = '20px';
        input.disabled = !!p.liquidate || (!p.carryApproved && data.checkedAt && p.acquiredDate !== data.checkedAt.slice(0, 10));
        input.addEventListener('change', async () => {
          editing = true; input.disabled = true;
          let saved = false;
          try {
            const response = await fetch('/api/positions/carry', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'TradingAgents'}, body: JSON.stringify({symbol: p.symbol, cycle: p.cycle, allowed: input.checked})});
            const result = await response.json(); if (!response.ok) throw new Error(result.error);
            saved = true;
          } catch (error) { el('positions-warning').textContent = error.message; input.checked = p.carryApproved; }
          finally { editing = false; input.disabled = false; if (saved) await refresh(); }
        });
        cell.append(input); row.append(cell);
        row.append(text('td', p.alert || (p.liquidate ? '청산 진행 · ' + p.exitReason : p.carryApproved ? '익일 청산 대상' : '당일 청산 대상')));
        return row;
      });
      if (!rows.length) { const row = document.createElement('tr'); const cell = text('td', '관리 중인 보유분 없음'); cell.colSpan = 9; row.append(cell); rows.push(row); }
      el('positions-body').replaceChildren(...rows);
      el('entry-checks').replaceChildren(...Object.entries(data.entryChecks || {}).map(([symbol, check]) => text('p', symbol + ' · ' + (check.allowed ? '분봉 진입 조건 통과' : '매수 보류: ' + (check.reasons || []).join(', ')) + ' · ' + check.at)));
      el('positions-warning').textContent = [data.error, data.journalError ? '보조 주문 로그 기록 오류: ' + data.journalError : '', data.valuationError ? '시세/평가 미확인: ' + data.valuationError : '', (data.unconfirmed || []).length ? '접수 불확실 주문: 신규 주문 차단' : '', Object.keys(data.unmanaged || {}).length ? '자동 관리 제외 보유분: ' + Object.entries(data.unmanaged).map(([s, q]) => s + ' ' + q + '주').join(', ') : ''].filter(Boolean).join(' · ');
      const orders = Object.values(data.orders || {}).slice(-30).reverse().map(o => {
        const row = document.createElement('tr');
        [o.payload.symbol, o.payload.side, o.payload.quantity || o.payload.orderAmount + '원', o.appliedQty, o.status, o.orderId || '접수 미확인'].forEach(v => row.append(text('td', v)));
        return row;
      });
      el('position-orders').replaceChildren(...orders);
    } catch (error) { el('positions-status').textContent = error.message; }
  }
  el('position-adopt-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button'); button.disabled = true;
    const payload = Object.fromEntries(new FormData(form)); payload.confirmed = form.elements.confirmed.checked;
    try {
      const response = await fetch('/api/positions/adopt', {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'TradingAgents'}, body: JSON.stringify(payload)});
      const result = await response.json(); if (!response.ok) throw new Error(result.error);
      el('position-adopt-message').textContent = '편입 완료. 다음 점검부터 청산 규칙이 적용됩니다.';
      form.reset(); await refresh();
    } catch (error) { el('position-adopt-message').textContent = error.message; }
    finally { button.disabled = false; }
  });
  refresh(); setInterval(refresh, 15000);
})();

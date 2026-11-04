(() => {
  const el = id => document.getElementById(id);
  if (!el('strategy-panel')) return;
  let state = {}, pending = false, signature = '', costRevision = null;
  const form = el('strategy-cost-form');
  const node = (tag, value) => { const result = document.createElement(tag); if (value !== undefined) result.textContent = value ?? '-'; return result; };
  const pct = value => value == null ? '-' : (Number(value) * 100).toFixed(3) + '%';
  const strategies = {baseline: '현재 전략', time_volume: '동일 시간대 거래량', breakout_retest: '돌파 후 재확인'};
  const outcomes = {target: '목표', stop: '손절', ambiguous: '순서 불명', incomplete: '미완료', unavailable: '자료 부족',
    blocked: '조건 미충족', 'entry-invalidated': '다음 봉 진입 무효', stagnation: '정체', liquidation: '청산'};
  function table(parent, headers, rows) {
    const wrapper = node('div'); wrapper.className = 'candidate-scroll';
    const table = node('table'), head = node('thead'), body = node('tbody'), tr = node('tr');
    headers.forEach(value => tr.append(node('th', value))); head.append(tr);
    rows.forEach(row => { const tr = node('tr'); row.forEach(value => tr.append(node('td', value))); body.append(tr); });
    table.append(head, body); wrapper.append(table); parent.append(wrapper);
  }
  function details(parent, label) { const d = node('details'); d.append(node('summary', label)); parent.append(d); return d; }
  function draw() {
    const busy = pending || !!state.active;
    el('strategy-run').disabled = busy || !state.available;
    el('strategy-cost-save').disabled = busy || !state.available;
    el('strategy-paper-apply').disabled = busy || state.mode !== 'dry-run' || !state.paperCosts;
    el('strategy-status').textContent = state.error || (state.active ? (state.active === 'observe' ? '5분 관찰 자료 수집 중' : '저장 자료 비교 중') :
      !state.available ? '자동 매매 실행기 재시작 필요' : '주문 없는 비교 · 매매 전략 미변경');
    el('strategy-watch').textContent = `5분 관찰 ${state.observationEnabled ? '활성' : '비활성'} · 후보 ${(state.watching || []).join(', ') || '없음'}`;
    const paper = state.paperCosts;
    el('strategy-paper-costs').textContent = paper ? `모의 계좌 v${paper.revision}: 편도 수수료 ${pct(paper.rates.feeRate)} · 매도 세금 ${pct(paper.rates.sellTaxRate)} · 편도 슬리피지 ${pct(paper.rates.slippageRate)}` : '모의 계좌 비용 적용 불가';
    if (state.profile && costRevision !== state.profile.revision) {
      Object.entries(state.profile.rates).forEach(([key, value]) => { if (form.elements[key]) form.elements[key].value = value; });
      costRevision = state.profile.revision;
    }
    el('strategy-cost-status').textContent = state.profile ? `비교 기준 v${state.profile.revision} · 왕복 비용 ${pct(state.profile.rates.effectiveRoundTrip)} 이상 · 모의 계좌와 별도` : '';
    const report = state.report, next = JSON.stringify(report);
    if (next === signature) return;
    signature = next;
    const root = el('strategy-result'); root.replaceChildren();
    if (!report) { root.append(node('p', '저장된 비교 결과 없음')); return; }
    root.append(node('h3', `${report.dates[0] || '-'} ~ ${report.dates.at(-1) || '-'} · ${report.mode === 'live' ? '실투자 자료' : '모의투자 자료'}`));
    root.append(node('p', `관측 ${report.observations.length}건 · 시간대 거래량 충족 ${report.timeVolumeReady}건 · 과거 자료 ${report.historyDays}일 · 비용 v${report.costRevision}`));
    const warnings = details(root, '해석 범위 · 자료 상태');
    const ul = node('ul'); report.limitations.forEach(value => ul.append(node('li', value))); warnings.append(ul);
    warnings.append(node('p', `원자료 오류 ${report.sourceIssues.length}건 · 평가 제외 ${report.errors.length}건`));
    if (report.sourceIssues.length) table(warnings, ['날짜', '원자료 상태'], report.sourceIssues.slice(0, 20).map(r => [r.date, r.error]));
    if (report.errors.length) table(warnings, ['시각 / 종목', '평가 제외'], report.errors.slice(0, 20).map(r => [r.at + ' / ' + r.ticker, r.error]));
    table(root, ['전략', '관측', '평가 가능', '기술 통과', '경로 측정', '평균 가상 순수익률'], Object.entries(report.totals).map(([name, r]) =>
      [strategies[name], r.observations, r.evaluated, r.signals, r.measuredPaths, pct(r.meanEstimatedNetReturn)]));
    root.append(node('p', '평균 가상 수익률은 겹치는 신호의 차트 추정치이며 계좌 수익률·실제 승률이 아닙니다.'));
    Object.entries(report.totals).forEach(([name, r]) => {
      const d = details(root, strategies[name] + ' · 결과와 보류 사유');
      table(d, ['차트 결과', '건수'], Object.entries(r.outcomes).map(([key, n]) => [outcomes[key] || key, n]));
      table(d, ['보류 사유', '횟수'], r.reasons);
    });
    const samples = details(root, '최근 관측 100건');
    table(samples, ['시각', '종목', '동일 시간대 거래량', '유효 일수', '왕복 비용', '현재 / 시간대 / 재확인'], report.observations.slice(-100).reverse().map(r =>
      [r.at, r.ticker, r.timeVolume.ratio == null ? '자료 부족' : r.timeVolume.ratio.toFixed(2) + '배', r.timeVolume.samples,
       pct(r.effectiveCostRate),
       Object.values(r.results).map(v => v.allowed == null ? '미평가' : v.allowed ? '기술 통과' : '보류').join(' / ')]));
    root.append(node('p', report.reportPath));
  }
  async function refresh() {
    try {
      const response = await fetch('/api/strategy', {cache: 'no-store'});
      if (response.status === 401) { location.assign('/login'); return; }
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '전략 비교 조회 실패');
      state = result; draw();
    } catch (error) { el('strategy-status').textContent = error.message; }
  }
  async function request(action, payload = {}) {
    if (pending) return;
    pending = true; draw();
    try {
      const response = await fetch('/api/strategy/' + action, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'TradingAgents'}, body: JSON.stringify(payload)});
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '요청 실패');
      state = result;
    } catch (error) { state.error = error.message; }
    finally { pending = false; draw(); }
  }
  const rates = () => Object.fromEntries(new FormData(form));
  form.addEventListener('submit', event => { event.preventDefault(); request('costs', {revision: costRevision, rates: rates()}); });
  el('strategy-run').addEventListener('click', () => request('run'));
  el('strategy-paper-apply').addEventListener('click', () => {
    if (!form.reportValidity()) return;
    const values = rates();
    if (!confirm(`모의 계좌의 이후 체결에 수수료 ${pct(values.feeRate)}, 매도 세금 ${pct(values.sellTaxRate)}, 편도 슬리피지 ${pct(values.slippageRate)}를 적용할까요? 비교용 왕복 하한은 계좌에 적용되지 않습니다. 자동 분석 일시정지, 보유·진행 주문 없음이 필요합니다. 과거 이력과 실투자는 변경하지 않습니다.`)) return;
    request('paper-costs', {rates: values, paperRevision: state.paperCosts?.revision, confirmed: true});
  });
  refresh(); setInterval(refresh, 5000);
})();

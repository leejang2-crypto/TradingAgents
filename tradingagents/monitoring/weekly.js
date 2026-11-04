(() => {
  const el = id => document.getElementById(id);
  if (!el('weekly-panel')) return;
  let state = {}, signature = '', pending = false;
  const text = (tag, value) => { const node = document.createElement(tag); node.textContent = value ?? '-'; return node; };
  const money = value => value == null ? '미확인' : Number(value).toLocaleString('ko-KR', {maximumFractionDigits: 0}) + '원';
  const percent = value => value == null ? '-' : (Number(value) * 100).toFixed(2) + '%';
  const labels = {running: '진행 중', completed: '완료', partial: '일부 자료 누락', proposed: '규모 축소 제안', 'insufficient-evidence': '변경 근거 부족',
    approved: '승인됨', revoked: '원복됨', rising: '상승', falling: '하락', flat: '횡보', unknown: '미확인'};
  const checks = {proposalExists: '학습 구간 손실 조건', sourceComplete: '원자료·미완결 보유 확인', heldOutSample: '검증 구간 10건·3일 이상',
    noOtherHoldings: '검증 구간 다른 규칙 보유 없음', unadjustedSource: '기존 규모 축소 없는 표본',
    sameTrades: '동일 체결·현금·정수 수량', lossReduction: '검증 구간 손실 축소', closedDrawdownNotWorse: '완결 손익 낙폭 비악화'};
  function list(parent, values) { const ul = document.createElement('ul'); values.forEach(value => ul.append(text('li', value))); parent.append(ul); }
  function detail(parent, title, open = false) {
    const node = document.createElement('details'); node.open = open; node.append(text('summary', title)); parent.append(node); return node;
  }
  function table(parent, headers, rows) {
    const wrapper = document.createElement('div'); wrapper.className = 'candidate-scroll';
    const node = document.createElement('table'), head = document.createElement('thead'), body = document.createElement('tbody');
    const title = document.createElement('tr'); headers.forEach(h => title.append(text('th', h))); head.append(title);
    rows.forEach(row => { const tr = document.createElement('tr'); row.forEach(v => tr.append(text('td', v))); body.append(tr); });
    if (!rows.length) { const tr = document.createElement('tr'), cell = text('td', '저장된 평가 자료 없음'); cell.colSpan = headers.length; tr.append(cell); body.append(tr); }
    node.append(head, body); wrapper.append(node); parent.append(wrapper);
  }
  function stats(parent, rows) {
    table(parent, ['구분', '매매 수', '순손익', '승률', '평균 이익', '평균 손실'], Object.entries(rows).map(([key, r]) =>
      [labels[key] || key, r.trades, money(r.netPnl), percent(r.winRate), money(r.averageWin), money(r.averageLoss)]));
  }
  function draw() {
    const report = state.report, busy = !!state.active || pending, comparison = state.comparison?.reportId === report?.id ? state.comparison : null;
    el('weekly-run').disabled = busy || !state.available;
    el('weekly-compare').disabled = busy || !report;
    el('weekly-summary').disabled = busy || !report;
    el('weekly-approve').disabled = busy || !comparison?.eligible || state.policy?.reportId === report?.id && state.policy?.status === 'approved';
    el('weekly-rollback').disabled = busy || state.policy?.status !== 'approved';
    el('weekly-status').textContent = state.error || (state.active ? '주간 복기 작업 진행 중: ' + state.active : !state.available ? '실행기 연결 필요' :
      report ? `${report.days[0]} ~ ${report.through} · ${report.mode === 'live' ? '실투자' : '모의투자'} · ${labels[report.status] || report.status} · ${report.updatedAt}` : '저장된 주간 복기 없음');
    el('weekly-policy').textContent = state.policy ? `주간 개선안 v${state.policy.revision} · ${labels[state.policy.status] || state.policy.status}` +
      (state.policy.expiresOn ? ` · 신규 진입 규모 ${Number(state.policy.multiplier) * 100}% · 만료 ${state.policy.expiresOn}` : '') +
      ` · 현재 ${state.effectiveSizing?.multiplier === '0.75' ? '적용 대상' : '미적용'}` : '승인된 주간 개선안 없음';
    const next = JSON.stringify([report, comparison, state.summary]);
    if (signature === next) return;
    signature = next;
    const root = el('weekly-result'); root.replaceChildren();
    if (!report) return;
    const d = report.diagnostics;
    root.append(text('h3', '최근 5거래일'));
    stats(root, {'확인된 완결 매매': d.actual});
    root.append(text('p', '자료 누락: ' + (report.missingDays.join(', ') || '없음')));
    const warnings = detail(root, '해석 범위와 자료 상태', report.status !== 'completed');
    list(warnings, report.limitations);
    list(warnings, report.sourceIssues.map(r => `${r.date}: ${r.error}`));
    list(warnings, d.unavailable);
    const proposal = detail(root, '개선안 · 모의 비교', true);
    proposal.append(text('p', `${labels[report.proposal.status] || report.proposal.status}: ${report.proposal.reason}`));
    proposal.append(text('p', `학습 ${report.proposal.training.trades}건 / ${report.proposal.training.days}일 · 검증 시작 ${report.proposal.validationFrom || '표본 부족'} · 비교 자본 ${money(report.proposal.capital)}`));
    if (comparison) {
      stats(proposal, {'기존 수량': comparison.baseline, '수량 75%': comparison.candidate});
      list(proposal, Object.entries(comparison.checks).map(([key, passed]) => `${checks[key] || key}: ${passed ? '충족' : '미충족'}`));
      proposal.append(text('p', comparison.eligible ? '제한적 규모 축소 승인 가능. 수익성 개선 검증이나 전체 전략 백테스트 결과가 아닙니다.' : '승인 불가: 표본·자료·비교 조건을 확인하세요.'));
    }
    list(proposal, report.recommendations);
    for (const [key, label] of [['byStrategy', '전략별 손익'], ['byEntryHour', '진입 시간대'], ['byEntryRegime', '진입 당시 KOSPI 흐름']]) stats(detail(root, label), d[key]);
    const hold = detail(root, 'HOLD와 차트 흐름');
    table(hold, ['항목', '후보·일 수'], [
      ['HOLD 후 규칙 구간 확인', d.hold.evaluatedCandidateDays || 0], ['목표 선도달 구간 포함', d.hold.targetAfterHold || 0],
      ['손절 선도달 구간 포함', d.hold.stopAfterHold || 0], ['동일 분봉 순서 불명', d.hold.ambiguousAfterHold || 0], ['자료 부족 후보', d.hold.unavailableCandidates || 0]]);
    hold.append(text('p', '동일 후보에 상승·하락 구간이 모두 있을 수 있습니다. 가상 구간 수는 실제 승률이 아닙니다.'));
    table(hold, ['전략', '목표', '손절', '순서 불명', '청산/정체', '미완료'], Object.entries(d.patterns).map(([key, r]) => [key, r.target || 0, r.stop || 0, r.ambiguous || 0, (r.liquidation || 0) + (r.stagnation || 0), r.incomplete || 0]));
    const exits = detail(root, `청산 흐름 · ${d.exitCoverage.measured}/${d.exitCoverage.trades}건 관측`);
    table(exits, ['날짜', '종목', '보유 중 최대 상승폭', '최대 하락폭', '실제 순수익률'], d.excursions.map(r => [r.date, r.symbol, percent(r.mfe), percent(r.mae), percent(r.netReturn)]));
    exits.append(text('p', '실제 보유 구간에 완전히 포함된 분봉만 관측합니다. 분봉 고가에서의 매도 가능성을 의미하지 않습니다.'));
    const system = detail(root, '실행 품질 · 뉴스 모델');
    system.append(text('p', `분석 시간 표본 ${d.latency.samples}건 · 평균 ${d.latency.meanSeconds?.toFixed(1) ?? '-'}초 · 최대 ${d.latency.maxSeconds?.toFixed(1) ?? '-'}초`));
    table(system, ['주문 상태', '건수'], Object.entries(d.orderStatuses));
    table(system, ['분석 오류 분류', '건수'], Object.entries(d.errors));
    table(system, ['뉴스 모델', '저장된 검토 수'], Object.entries(d.newsModels));
    table(system, ['차단 사유', '횟수'], d.blockReasons);
    const sources = detail(root, '원자료 · 후보군');
    table(sources, ['지수', '기준일', '종목 수'], d.universeSnapshots.map(r => [r.index, r.asOf, r.members]));
    list(sources, report.sources.map(r => `${r.date}: ${r.directory}`));
    sources.append(text('p', report.reportPath));
    if (state.summary?.reportId === report.id) {
      const summary = detail(root, '로컬 요약 · ' + state.summary.model, true);
      list(summary, state.summary.facts);
    }
  }
  async function refresh() {
    try {
      const response = await fetch('/api/weekly', {cache: 'no-store'});
      if (response.status === 401) { location.assign('/login'); return; }
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '주간 복기 조회 실패');
      state = result; draw();
    } catch (error) { el('weekly-status').textContent = error.message; }
  }
  async function act(action) {
    if (pending) return;
    let confirmed = false;
    if (action === 'approve') {
      confirmed = confirm(`${state.mode === 'live' ? '실투자' : '모의투자'} 신규 진입 규모를 최대 7일간 75%로 제한할까요? 과거 체결 수량 비교만 수행했으며 수익성 개선은 검증되지 않았습니다. 기존 보유·익절·손절·청산 기준은 유지합니다.`);
      if (!confirmed) return;
    }
    if (action === 'rollback') {
      confirmed = confirm('현재 주간 개선안 적용을 해제할까요? 신규 진입 규모가 원래 설정으로 돌아갈 수 있으며, 별도의 일별 복기 제한은 유지됩니다.');
      if (!confirmed) return;
    }
    pending = true; draw();
    try {
      const response = await fetch('/api/weekly/' + action, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'TradingAgents'},
        body: JSON.stringify({reportId: state.report?.id, revision: state.policy?.revision, confirmed})});
      if (response.status === 401) { location.assign('/login'); return; }
      const result = await response.json();
      if (!response.ok) throw new Error(result.error || '주간 복기 요청 실패');
      state = result;
    } catch (error) { state.error = error.message; }
    finally { pending = false; draw(); }
  }
  ['run', 'compare', 'summary', 'approve', 'rollback'].forEach(action => el('weekly-' + action).addEventListener('click', () => act(action)));
  refresh(); setInterval(refresh, 5000);
})();

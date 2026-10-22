(() => {
  const el = id => document.getElementById(id);
  const actions = {news: '종목 뉴스 수집', candidates: '후보 종목 선정', analyze: '종목 분석', trade: '자동 매매 (1회)'};
  const phases = {starting: '시작', analysis: '분석', 'llm-request': 'LLM 요청', 'data-request': '자료 요청',
    'news-collection': '뉴스 수집', 'candidate-discovery': '후보 선정', 'next-candidate': '다음 종목',
    'candidates-collected': '뉴스 후보 수집 완료', 'candidate-market-data': '후보 시장자료 조회',
    'candidate-screen': '단타 후보 평가', 'candidate-screened': '단타 후보 선정 완료',
    planning: '주문 계획', 'order-preparation': '주문 준비', 'order-submitting': '주문 준비',
    'order-sending': '주문 접수 요청', 'paper-fill': '가상 체결', 'paper-record': '모의 기록'};
  const statuses = {completed: '완료', stopped: '중단', failed: '실패', cancelled: '취소', interrupted: '재시작 전 중단', 'completed-with-errors': '일부 오류'};
  let state = null, command = null, sending = false;
  const headers = {'Content-Type': 'application/json', 'X-Requested-With': 'TradingAgents'};
  // getRandomValues works on Tailscale HTTP too; randomUUID requires a secure context.
  const requestId = () => Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join('');
  const jobName = job => job ? (actions[job.action] || job.action) + (job.ticker && ['news', 'analyze'].includes(job.action) ? ' · ' + job.ticker : '') : '-';
  function message(value) {
    el('control-message').textContent = value;
    el('auto-run-message').textContent = value;
  }
  function render(next) {
    state = next;
    el('manual-mode').textContent = !state.available ? '수동 실행기 없음' : state.mode === 'live' ? '실투자 · 실제 Toss 주문' : '모의투자';
    el('auto-paused').checked = !!state.paused;
    el('auto-paused').disabled = !state.available;
    el('current-job').textContent = jobName(state.active) + (state.active ? ' · ' + (state.stopRequested ? '중단 대기' : phases[state.active.phase] || state.active.phase) : '');
    el('pending-job').textContent = jobName(state.pending);
    el('cancel-pending').hidden = !state.pending;
    el('last-job').textContent = jobName(state.last) + (state.last ? ' · ' + (statuses[state.last.status] || state.last.status) + (state.last.result?.error ? ' · ' + state.last.result.error : '') : '');
    el('auto-run-mode').textContent = el('manual-mode').textContent;
    el('auto-run-state').textContent = !state.available ? 'daily-market 실행기 연결 필요' : [
      state.active ? '진행: ' + el('current-job').textContent : '진행 중인 작업 없음',
      state.pending ? '대기: ' + jobName(state.pending) : '',
      state.paused ? '예약 자동 실행 일시정지' : '',
      state.last ? '최근: ' + el('last-job').textContent : '',
      state.last?.result?.selectionReason || '',
    ].filter(Boolean).join(' · ');
    const result = state.last?.result;
    el('manual-result').hidden = !result;
    if (result) {
      const lines = [];
      if (result.error) lines.push('오류: ' + result.error);
      if (result.selectionReason) lines.push('선정 결과: ' + result.selectionReason);
      if (result.decision) lines.push('판단: ' + result.decision);
      if (result.articleCount != null) lines.push('뉴스 수: ' + result.articleCount);
      if (result.candidateCount != null) lines.push('후보 수: ' + result.candidateCount);
      for (const row of result.candidates || []) lines.push(row.ticker + ' · ' + row.name + ' · 점수 ' + row.score);
      for (const row of result.analyses || []) lines.push(row.ticker + ': ' + (row.error || row.decision || '-'));
      const path = result.reportPath || result.summaryPath || result.markdownPath || result.path;
      if (path) lines.push('저장 경로: ' + path);
      el('manual-result-text').textContent = lines.join('\n') || '완료';
    }
    document.querySelectorAll('[data-action]').forEach(button => { button.disabled = sending || !state.available || !!state.pending; });
    document.dispatchEvent(new Event('dashboard-data'));
  }
  async function refresh() {
    try {
      const response = await fetch('/api/control', {cache: 'no-store'});
      if (response.status === 401) { location.assign('/login'); return; }
      if (!response.ok) throw new Error('실행 상태 조회 실패');
      render(await response.json());
    } catch (error) { message(error.message); document.dispatchEvent(new Event('dashboard-data')); }
  }
  function dialog(kind, message) {
    el('run-dialog-title').textContent = kind === 'busy' ? '현재 작업 처리' : '실제 주문 실행 확인';
    el('run-dialog-message').textContent = message;
    el('run-stop').hidden = kind !== 'busy';
    el('run-wait').hidden = kind !== 'busy';
    el('run-confirm').hidden = kind === 'busy';
    el('run-dialog').showModal();
  }
  async function submit(policy = 'ask', activeId = null) {
    sending = true;
    if (state) render(state);
    message('실행 요청 중...');
    try {
      const response = await fetch('/api/control/run', {method: 'POST', headers, body: JSON.stringify({...command, policy, activeId})});
      const result = await response.json();
      if (response.status === 409 && result.control?.active && !result.control.pending) {
        render(result.control);
        command.activeId = result.control.active.id;
        dialog('busy', jobName(result.control.active) + ' 진행 중입니다. 진행 중 API 응답을 기다린 뒤 안전한 단계에서 중단합니다.');
        message('처리 방법 선택 대기');
        return;
      }
      if (!response.ok) throw new Error(result.error || '실행 요청 실패');
      render(result.control);
      message('실행 요청을 접수했습니다.');
      command = null;
    } catch (error) { message(error.message); }
    finally { sending = false; if (state) render(state); }
  }
  document.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => {
    if (sending || !state?.available || state.pending) return;
    command = {requestId: requestId(), action: button.dataset.action};
    // Whole-universe trading does not depend on the single-stock input field.
    if (command.action !== 'trade') command.ticker = el('manual-ticker').value.trim().toUpperCase();
    if (command.action === 'trade' && state.mode === 'live') {
      dialog('live', '저장된 매매 설정으로 후보를 선정하고 분석 결과에 따라 실제 주문을 전송합니다.');
    } else { submit(); }
  }));
  el('run-confirm').addEventListener('click', () => { el('run-dialog').close(); command.confirmLive = true; submit(); });
  el('run-stop').addEventListener('click', () => { el('run-dialog').close(); submit('stop', command.activeId); });
  el('run-wait').addEventListener('click', () => { el('run-dialog').close(); submit('wait', command.activeId); });
  el('run-dismiss').addEventListener('click', () => { el('run-dialog').close(); command = null; });
  el('auto-paused').addEventListener('change', async event => {
    try {
      const response = await fetch('/api/control/pause', {method: 'POST', headers, body: JSON.stringify({paused: event.target.checked})});
      const result = await response.json(); if (!response.ok) throw new Error(result.error);
      render(result.control);
    } catch (error) { message(error.message); await refresh(); }
  });
  el('cancel-pending').addEventListener('click', async () => {
    try {
      const response = await fetch('/api/control/cancel-pending', {method: 'POST', headers, body: JSON.stringify({requestId: state.pending?.id})});
      const result = await response.json(); if (!response.ok) throw new Error(result.error);
      render(result.control);
      message('대기 실행을 취소했습니다.');
    } catch (error) { message(error.message); await refresh(); }
  });
  el('logout').addEventListener('click', async () => {
    if (window.Dashboard && !window.Dashboard.canLogout()) return;
    const response = await fetch('/api/logout', {method: 'POST', headers, body: '{}'});
    if (response.ok || response.status === 401) { window.Dashboard?.saved(window.Dashboard.formSignature()); location.assign('/login'); }
  });
  refresh();
  setInterval(refresh, 3000);
})();

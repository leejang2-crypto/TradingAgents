(() => {
  const el = id => document.getElementById(id);
  const labels = {running: '진행 중', completed: '완료', failed: '실패', stopped: '중단', interrupted: '종료됨'};
  const horizons = {'1-3': '1~3개월', '3-12': '3~12개월', '12-36': '1~3년'};
  const indexLabel = universe => ({KOSPI100: '코스피100', KOSPI200: '코스피200'}[universe?.index] || '이전 사용자 지정 범위');
  const timestamp = value => value ? new Date(value).toLocaleString('ko-KR', {timeZone: 'Asia/Seoul', hour12: false}) : '-';
  const node = (tag, text) => { const item = document.createElement(tag); item.textContent = text; return item; };
  const headers = {'Content-Type': 'application/json', 'X-Requested-With': 'TradingAgents'};
  const filterKeys = ['max_pe', 'min_revenue_growth_pct', 'max_debt_equity', 'max_drawdown_pct', 'max_dilution_pct', 'min_turnover_krw', 'min_quality_score'];
  const numeric = (value, percent = false) => value == null ? '-' : percent ? (value * 100).toFixed(2) + '%' : Number(value).toLocaleString('ko-KR', {maximumFractionDigits: 2});
  let state = null, sending = false, version = 0, reportKey = '', historyKey = '';
  function buttons() {
    el('research-start').disabled = sending || !state?.available || !!state?.active;
    el('research-stop').disabled = sending || !state?.canStop;
    el('research-universe-save').disabled = sending || !state?.available || !!state?.active;
  }
  function render(data) {
    state = data; buttons();
    const universe = data.universe;
    const uploaded = universe?.source === '사용자 확인 KRX CSV';
    el('research-universe-file-info').textContent = uploaded
      ? `파일명: ${universe.fileName || '미기록 (이전 등록 자료)'}\n조회 기준일: ${universe.asOf || '-'}\n등록 일시 (한국 시간): ${universe.registeredAt ? timestamp(universe.registeredAt) : (universe.checkedOn || '-') + ' (시각 미기록)'}` : '';
    const imported = universe?.importInfo;
    el('research-import-info').textContent = imported
      ? `등록 성공 · ${imported.format} ${imported.rowCount}종목 · 앞자리 0 복원 ${imported.zeroPaddedCodes}건 · 영문 포함 코드 ${imported.alphanumericCodes}건${universe.nonstandardCountConfirmed && imported.rowCount === 201 ? ' · 201종목 원본 확인됨' : ''}` : '';
    el('research-universe-state').textContent = universe?.members
      ? `${indexLabel(universe)} · ${Object.keys(universe.members).length}종목 · 기준일 ${universe.asOf} · 확인일 ${universe.checkedOn} · ${universe.source}${universe.index !== 'KOSPI200' ? ' · 새 분석에는 코스피200 구성목록 등록 필요' : uploaded ? ' · 다음 CSV 등록 전까지 계속 사용' : ''}`
      : '코스피200 구성목록 미등록 · 자동 조회 실패 시 KRX CSV 필요';
    const status = data.active || data.status;
    el('research-state').textContent = !data.available ? '중장기 탐색 실행기 없음' : status?.id
      ? `${labels[status.status] || status.status} · ${status.phase || ''} · ${timestamp(status.updatedAt)}` : '탐색 기록 없음';
    const history = JSON.stringify(data.history || []);
    if (history !== historyKey) {
      historyKey = history;
      const select = el('research-history'), selected = select.value;
      const options = [node('option', '최근 결과')]; options[0].value = '';
      (data.history || []).forEach(job => {
        const option = node('option', `${timestamp(job.updatedAt)} · ${labels[job.status] || job.status}`);
        option.value = job.id; options.push(option);
      });
      if (selected && !options.some(option => option.value === selected)) {
        const old = node('option', selected); old.value = selected; options.push(old);
      }
      select.replaceChildren(...options); select.value = selected;
    }
    const report = data.report, key = JSON.stringify(report);
    if (key === reportKey) return;
    reportKey = key; el('research-results').hidden = !report;
    if (!report) return;
    el('research-meta').textContent = `${timestamp(report.createdAt)} · ${horizons[report.horizon]} · ${indexLabel(report.universe)}${report.universe ? ' (구성 기준일 ' + report.universe.asOf + ')' : ''} · 조회 ${report.scope.length}종목 · 기준 통과 ${report.rows.filter(row => row.eligible).length}종목 · 상위 후보 ${report.shortlist.length}종목 · ${report.scoringVersion}`;
    el('research-llm').textContent = `LLM: ${report.provider}${report.model ? ' / ' + report.model : ''} · ${report.llmStatus}`;
    const f = report.filters;
    el('research-filter-summary').textContent = f ? `저장 조건: PER ≤ ${f.max_pe} · 매출 YoY > ${f.min_revenue_growth_pct}% · 부채비율 ≤ ${f.max_debt_equity}% · 낙폭 ≤ ${f.max_drawdown_pct}% · 주식 수 증가 ≤ ${f.max_dilution_pct}% · 거래대금 ≥ ${numeric(f.min_turnover_krw)} KRW · 검증 점수 ≥ ${f.min_quality_score}` : '이전 버전 결과: 추가 검증 기준 미적용';
    el('research-limitations').replaceChildren(...report.limitations.map(text => node('li', text)));
    el('research-failures').replaceChildren(...report.errors.map(error => node('li', `${error.ticker}: ${error.error}`)));
    const rows = report.rows.map(row => {
      const tr = document.createElement('tr');
      [row.name + ' · ' + row.ticker, row.classification,
        row.quality ? `${row.rankingScore} / ${row.score} / ${row.quality.score}` : `${row.score} (기존)`,
        row.quality ? `${row.coverage}% / ${row.quality.coverage}%` : row.coverage + '%',
        (row.priceDate || '-') + ' / ' + (row.financialDate || '-')].forEach(value => tr.append(node('td', value)));
      const cell = document.createElement('td'), details = document.createElement('details');
      details.append(node('summary', '근거 · 누락 · 검토'));
      const body = document.createElement('div'); body.className = 'research-evidence';
      body.append(node('p', `업종: ${row.sector || '-'} · 재무 통화: ${row.currency || '-'}`));
      const list = document.createElement('ul');
      row.facts.forEach(fact => list.append(node('li', `${fact.text} · ${fact.weight ? `${fact.points}/${fact.weight}점` : '참고 비교'} · ${fact.source}${fact.peers ? ' · ' + fact.peers.join(', ') : ''}`)));
      row.missing.forEach(text => list.append(node('li', '자료 누락: ' + text)));
      row.warnings.forEach(text => list.append(node('li', '주의: ' + text)));
      (row.quality?.missing || []).forEach(text => list.append(node('li', '추가 자료 부족: ' + text)));
      (row.quality?.risks || []).forEach(text => list.append(node('li', '추가 위험: ' + text)));
      (row.quality?.errors || []).forEach(text => list.append(node('li', text)));
      body.append(list);
      if (row.peerComparison) body.append(node('p', `동종업종: ${row.industry || '-'} · ${row.peerComparison.note}`));
      if (row.quality?.quarters?.length) {
        const quarterDetails = document.createElement('details');
        quarterDetails.append(node('summary', `분기 실적 ${row.quality.quarters.length}개 · 공급자 재무 통화`));
        const scroll = document.createElement('div'); scroll.className = 'account-scroll';
        const table = document.createElement('table'); table.className = 'research-quarter-table';
        const head = document.createElement('thead'), header = document.createElement('tr');
        ['기준일', '매출', '매출 YoY', '영업이익률', '영업현금흐름', '잉여현금흐름'].forEach(label => header.append(node('th', label)));
        head.append(header); table.append(head);
        const tbody = document.createElement('tbody');
        row.quality.quarters.forEach(q => {
          const line = document.createElement('tr');
          [q.date, numeric(q.revenue), numeric(q.revenueYoY, true), numeric(q.operatingMargin, true), numeric(q.operatingCashFlow), numeric(q.freeCashFlow)].forEach(value => line.append(node('td', value)));
          tbody.append(line);
        });
        table.append(tbody); scroll.append(table); quarterDetails.append(scroll); body.append(quarterDetails);
      }
      if (row.review) {
        body.append(node('h3', 'LLM이 선택한 검토 근거'));
        const evidence = new Map(row.facts.map(fact => [fact.id, fact.text]));
        row.review.supporting_ids.forEach(id => body.append(node('p', '긍정 근거: ' + evidence.get(id))));
        row.review.risk_ids.forEach(id => body.append(node('p', '위험 근거: ' + evidence.get(id))));
        row.review.question_ids.forEach(id => body.append(node('p', '추가 확인: ' + report.questions[id])));
      }
      details.append(body);
      cell.append(details); tr.append(cell); return tr;
    });
    el('research-rows').replaceChildren(...rows);
    document.dispatchEvent(new Event('dashboard-data'));
  }
  async function refresh() {
    if (sending) return;
    const current = ++version;
    try {
      const query = new URLSearchParams();
      if (el('research-history').value) query.set('id', el('research-history').value);
      const response = await fetch('/api/research?' + query, {cache: 'no-store'});
      if (!response.ok) throw new Error(response.status === 401 ? '로그인이 필요합니다.' : '탐색 상태 조회 실패');
      const data = await response.json();
      if (current === version) render(data);
    } catch (error) { if (current === version) el('research-error').textContent = error.message; }
  }
  async function command(action, payload) {
    sending = true; ++version; buttons(); el('research-error').textContent = '';
    try {
      const response = await fetch('/api/research/' + action, {method: 'POST', headers, body: JSON.stringify(payload)});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '탐색 요청 실패');
      el('research-history').value = ''; render(data);
    } catch (error) { el('research-error').textContent = error.message; }
    finally { sending = false; buttons(); await refresh(); }
  }
  el('research-form').addEventListener('submit', event => {
    event.preventDefault();
    if (sending || state?.active) return;
    const form = new FormData(event.currentTarget);
    const provider = form.get('research_provider');
    if (provider !== 'none' && !confirm(provider === 'openai' ? '중장기 탐색에 OpenAI API 비용이 발생할 수 있습니다. 실행할까요?' : 'Ollama는 단타 분석과 Mac의 메모리·연산 자원을 공유합니다. 실행할까요?')) return;
    command('run', {requestId: Array.from(crypto.getRandomValues(new Uint8Array(16)), n => n.toString(16).padStart(2, '0')).join(''),
      provider, horizon: form.get('research_horizon'), tickers: form.get('research_tickers'),
      filters: Object.fromEntries(filterKeys.map(key => [key, Number(form.get('research_' + key))]))});
  });
  el('research-form').addEventListener('invalid', event => {
    let parent = event.target.parentElement;
    while (parent && parent !== el('research-form')) { if (parent.tagName === 'DETAILS') parent.open = true; parent = parent.parentElement; }
  }, true);
  el('research-universe-form').addEventListener('submit', async event => {
    event.preventDefault();
    if (sending || state?.active) return;
    try {
      const form = new FormData(event.currentTarget);
      const file = el('research-universe-file').files[0];
      if (!file || file.size > 120000) throw new Error('120 KB 이하의 KRX CSV를 선택하세요.');
      const bytes = await file.arrayBuffer();
      let csv;
      try { csv = new TextDecoder('utf-8', {fatal: true}).decode(bytes); }
      catch (_) { csv = new TextDecoder('euc-kr', {fatal: true}).decode(bytes); }
      await command('universe', {csv, fileName: file.name, asOf: form.get('asOf'), confirmed: form.get('confirmed') === 'on',
        nonstandardCountConfirmed: form.get('nonstandardCountConfirmed') === 'on'});
    } catch (error) { el('research-error').textContent = error.message; }
  });
  el('research-stop').addEventListener('click', () => {
    if (state?.active && confirm('중장기 탐색만 중단할까요? 단타 매매와 보유 점검은 계속됩니다.'))
      command('stop', {requestId: state.active.id});
  });
  el('research-history').addEventListener('change', refresh);
  refresh();
  setInterval(() => { if (!el('view-research')?.hidden || state?.active) refresh(); }, 5000);
})();

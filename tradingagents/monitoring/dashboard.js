/* Presentation state only. Trading settings/account data never enter browser storage. */
(() => {
  const el = id => document.getElementById(id);
  const storageKey = 'tradingagents-dashboard-layout-v1';
  let saved = {};
  try { saved = JSON.parse(sessionStorage.getItem(storageKey) || '{}') || {}; } catch (_) {}
  const views = ['overview', 'analysis', 'research', 'history', 'execution', 'settings'];
  let current = views.includes(saved.view) ? saved.view : 'overview';
  const scrolls = saved.scrolls && typeof saved.scrolls === 'object' ? saved.scrolls : {};
  const folds = saved.folds && typeof saved.folds === 'object' ? saved.folds : {};
  const filters = new Map();
  const form = el('settings-form');
  const formSignature = () => JSON.stringify(Array.from(new FormData(form).entries()));
  let baseline = formSignature();
  const dirty = () => formSignature() !== baseline;
  const text = (tag, value, className) => {
    const node = document.createElement(tag); node.textContent = value;
    if (className) node.className = className;
    return node;
  };
  function persist() {
    try { sessionStorage.setItem(storageKey, JSON.stringify({view: current, scrolls, folds})); } catch (_) {}
  }
  function setDirty() { el('settings-dirty').textContent = dirty() ? '저장하지 않은 변경사항' : ''; }
  function restoreFolds(root) {
    root.querySelectorAll('details[data-detail-key]').forEach(detail => {
      if (typeof folds[detail.dataset.detailKey] === 'boolean') detail.open = folds[detail.dataset.detailKey];
    });
  }
  function show(view, {force = false, focus = false} = {}) {
    if (!views.includes(view)) return false;
    if (!force && current === 'settings' && view !== current && dirty()
        && !confirm('저장하지 않은 설정이 있습니다. 입력을 유지한 채 다른 탭으로 이동할까요?')) return false;
    scrolls[current] = window.scrollY;
    current = view;
    views.forEach(name => {
      const active = name === current;
      el('view-' + name).hidden = !active;
      el('tab-' + name).setAttribute('aria-selected', String(active));
      el('tab-' + name).tabIndex = active ? 0 : -1;
    });
    persist();
    if (focus) el('tab-' + view).focus({preventScroll: true});
    const y = Number(scrolls[view]);
    requestAnimationFrame(() => window.scrollTo({top: Number.isFinite(y) && y >= 0 ? y : 0, behavior: 'instant'}));
    return true;
  }

  const main = el('dashboard-main');
  views.forEach(name => {
    const panel = document.createElement('div');
    panel.id = 'view-' + name; panel.className = 'dashboard-view';
    panel.setAttribute('role', 'tabpanel'); panel.setAttribute('aria-labelledby', 'tab-' + name);
    panel.tabIndex = 0; main.append(panel);
  });
  const move = (selector, view) => el('view-' + view).append(document.querySelector(selector));
  move('[data-live="runtime"]', 'overview');
  move('[data-live="objective"]', 'overview');
  move('#positions-panel', 'overview');
  move('[data-live="analyses"]', 'analysis');
  move('[data-live="candidates"]', 'analysis');
  move('#research-panel', 'research');
  move('#account-panel', 'history');
  const orders = document.createElement('section');
  orders.append(el('position-order-panel')); el('view-history').append(orders);
  move('[data-live="trades"]', 'history');
  move('#manual-panel', 'execution');
  el('manual-panel').append(el('position-adoption'));
  move('#settings-panel', 'settings');
  const preview = document.createElement('section'); preview.id = 'account-preview';
  preview.append(text('h2', '계좌 요약'));
  const source = text('p', '계좌 자료 조회 전', 'muted'); source.id = 'account-preview-source';
  const summary = document.createElement('div'); summary.id = 'account-preview-metrics'; summary.className = 'grid';
  const openAccount = text('button', '계좌 · 수익률 조회'); openAccount.type = 'button';
  openAccount.addEventListener('click', () => show('history'));
  preview.append(source, summary, openAccount); el('view-overview').insertBefore(preview, el('positions-panel'));
  // Native details keep every setting in the same form, including closed groups.
  restoreFolds(document);
  el('dashboard-tabs').hidden = false;
  document.body.classList.add('dashboard-ready');
  const initial = current;
  const initialScroll = scrolls[initial];
  show(initial, {force: true});
  scrolls[initial] = initialScroll;
  requestAnimationFrame(() => window.scrollTo(0, Number(initialScroll) || 0));
  document.querySelectorAll('[role="tab"]').forEach(tab => {
    tab.addEventListener('click', () => show(tab.dataset.view));
    tab.addEventListener('keydown', event => {
      let index = views.indexOf(tab.dataset.view);
      if (event.key === 'ArrowRight') index = (index + 1) % views.length;
      else if (event.key === 'ArrowLeft') index = (index + views.length - 1) % views.length;
      else if (event.key === 'Home') index = 0;
      else if (event.key === 'End') index = views.length - 1;
      else return;
      event.preventDefault(); show(views[index], {focus: true});
    });
  });
  document.addEventListener('toggle', event => {
    const key = event.target.dataset?.detailKey;
    if (key) { folds[key] = event.target.open; persist(); }
  }, true);
  form.addEventListener('input', setDirty);
  form.addEventListener('change', setDirty);
  form.addEventListener('invalid', event => {
    show('settings', {force: true});
    let parent = event.target.parentElement;
    while (parent && parent !== form) {
      if (parent.tagName === 'DETAILS') parent.open = true;
      parent = parent.parentElement;
    }
  }, true);
  window.addEventListener('beforeunload', event => {
    if (dirty()) { event.preventDefault(); event.returnValue = ''; }
  });
  window.addEventListener('pagehide', () => { scrolls[current] = window.scrollY; persist(); });
  const summaryDialog = el('summary-dialog');
  let summaryRequest = null;
  let summaryTrigger = null;
  el('summary-close').addEventListener('click', () => summaryDialog.close());
  summaryDialog.addEventListener('close', () => {
    summaryRequest?.abort(); summaryRequest = null;
    el('summary-content').textContent = '';
    (summaryTrigger?.isConnected ? summaryTrigger : el('tab-analysis')).focus({preventScroll: true});
  });
  // Delegate clicks because polling replaces the analysis table and its link.
  document.addEventListener('click', async event => {
    const link = event.target.closest('a[data-summary-link]');
    if (!link || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    summaryRequest?.abort();
    const request = new AbortController(); summaryRequest = request; summaryTrigger = link;
    el('summary-file').textContent = link.textContent;
    el('summary-content').textContent = ''; el('summary-content').hidden = true;
    el('summary-status').textContent = '불러오는 중...';
    summaryDialog.showModal();
    try {
      const response = await fetch(link.href, {signal: request.signal, cache: 'no-store'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '분석 요약을 불러오지 못했습니다.');
      if (summaryRequest !== request || !summaryDialog.open) return;
      el('summary-content').textContent = JSON.stringify(data.content, null, 2);
      el('summary-content').hidden = false;
      el('summary-content').scrollTop = 0;
      el('summary-status').textContent = '';
    } catch (error) {
      if (error.name !== 'AbortError' && summaryRequest === request && summaryDialog.open)
        el('summary-status').textContent = error.message;
    }
  });
  const headerObserver = new ResizeObserver(() => {
    document.documentElement.style.setProperty('--dashboard-header-height', el('dashboard-header').offsetHeight + 'px');
  });
  headerObserver.observe(el('dashboard-header'));

  const tableConfigs = [
    {key: 'research-results', root: '#research-results > .account-scroll', symbol: 0, outside: true},
    {key: 'recent-trades', root: '[data-live="trades"]', symbol: 1, date: 0},
    {key: 'position-orders', root: '#position-order-panel', symbol: 0},
    {key: 'account-orders', root: '#account-orders', symbol: 1, date: 0, outside: true},
    {key: 'account-holdings', root: '#account-holdings', symbol: 0, outside: true},
    {key: 'account-returns', root: '#account-returns', outside: true},
  ];
  function enhanceTables() {
    tableConfigs.forEach(config => {
      const root = document.querySelector(config.root);
      const table = root?.querySelector('table');
      let controls = el('table-tools-' + config.key);
      if (!table) { if (controls) controls.hidden = true; return; }
      let state = filters.get(config.key);
      if (!state) { state = {query: '', from: '', through: '', page: 1}; filters.set(config.key, state); }
      if (!controls) {
        controls = document.createElement('div'); controls.id = 'table-tools-' + config.key; controls.className = 'table-tools';
        const field = (key, label, type) => {
          const wrapper = text('label', label), input = document.createElement('input');
          input.type = type; input.value = state[key]; input.dataset.filter = key;
          input.setAttribute('aria-label', config.key + ' ' + label);
          input.addEventListener('input', () => { state[key] = input.value; state.page = 1; enhanceTables(); });
          wrapper.append(input); controls.append(wrapper);
        };
        if (config.symbol != null) field('query', '종목 코드', 'search');
        if (config.date != null) { field('from', '시작일', 'date'); field('through', '종료일', 'date'); }
        const pagination = document.createElement('div'); pagination.className = 'table-pagination';
        const previous = text('button', '‹'), next = text('button', '›'), status = text('span', '');
        previous.type = next.type = 'button';
        previous.title = '이전 페이지'; previous.setAttribute('aria-label', config.key + ' 이전 페이지');
        next.title = '다음 페이지'; next.setAttribute('aria-label', config.key + ' 다음 페이지');
        previous.dataset.page = 'previous'; next.dataset.page = 'next'; status.dataset.page = 'status';
        previous.addEventListener('click', () => { state.page--; enhanceTables(); });
        next.addEventListener('click', () => { state.page++; enhanceTables(); });
        pagination.append(previous, status, next); controls.append(pagination);
        if (config.outside) root.before(controls); else root.insertBefore(controls, table.closest('.account-scroll') || table);
      }
      controls.hidden = false;
      const rows = Array.from(table.tBodies[0]?.rows || []);
      const actual = rows.filter(row => row.cells.length > 1);
      const invalidRange = state.from && state.through && state.from > state.through;
      const matching = actual.filter(row => {
        const symbol = config.symbol == null ? '' : row.cells[config.symbol]?.textContent.toUpperCase() || '';
        const date = config.date == null ? '' : row.cells[config.date]?.textContent.slice(0, 10) || '';
        return !invalidRange && symbol.includes(state.query.trim().toUpperCase()) && (!state.from || date >= state.from) && (!state.through || date <= state.through);
      });
      const pages = Math.max(1, Math.ceil(matching.length / 10));
      state.page = Math.min(pages, Math.max(1, state.page));
      const visible = new Set(matching.slice((state.page - 1) * 10, state.page * 10));
      actual.forEach(row => { row.hidden = !visible.has(row); });
      controls.querySelector('[data-page="previous"]').disabled = state.page <= 1;
      controls.querySelector('[data-page="next"]').disabled = state.page >= pages;
      controls.querySelector('[data-page="status"]').textContent = invalidRange ? '날짜 범위 확인 필요' : `${state.page} / ${pages} · 조회된 ${actual.length}건 중 ${matching.length}건`;
    });
  }
  function accountPreview() {
    const ready = !el('account-results').hidden;
    el('account-preview-source').textContent = ready ? el('account-source').textContent : '계좌 자료 조회 전';
    el('account-preview-metrics').replaceChildren(...(ready ? Array.from(el('account-metrics').children).map(node => node.cloneNode(true)) : []));
  }
  function globalStatus() {
    const job = el('current-job').textContent;
    const paused = el('auto-paused').checked;
    el('global-job').textContent = (job === '-' ? '진행 중인 분석 없음' : job) + (paused ? ' · 자동 분석 일시정지' : '')
      + (el('pending-job').textContent !== '-' ? ' · 대기 작업 있음' : '');
    const warnings = [el('positions-warning').textContent, el('positions-status').textContent.includes('실패') ? el('positions-status').textContent : '',
      /실패|오류/.test(el('control-message').textContent) ? el('control-message').textContent : ''];
    document.querySelectorAll('#positions-body tr').forEach(row => {
      if (row.cells.length < 2) return;
      const status = row.lastElementChild?.textContent || '';
      if (/대기|불일치|미확인|없음|실패|필요|초과/.test(status)) warnings.push(row.firstElementChild.textContent + ': ' + status);
    });
    if (/한도 도달|한도 · 매수 중단/.test(el('positions-daily').textContent)) warnings.push('당일 손실 또는 연속 손절 한도: 신규 매수 중단');
    const relevant = warnings.filter(Boolean);
    const warning = el('global-warning'); warning.hidden = !relevant.length;
    warning.textContent = relevant.length ? '점검 필요 · ' + relevant[0].slice(0, 110) + (relevant.length > 1 ? ` · ${relevant.length}개 경고` : '') : '';
    warning.title = relevant.join('\n');
  }
  el('global-warning').addEventListener('click', () => {
    if (show('overview')) requestAnimationFrame(() => el('positions-panel').scrollIntoView({block: 'start'}));
  });
  document.addEventListener('dashboard-data', () => { enhanceTables(); accountPreview(); globalStatus(); });
  window.Dashboard = {
    formSignature,
    canLogout: () => !dirty() || confirm('저장하지 않은 설정이 있습니다. 저장하지 않고 로그아웃할까요?'),
    saved(signature) { if (typeof signature === 'string') baseline = signature; setDirty(); },
    replaceLive(panel, replacement) {
      if (panel.contains(document.activeElement) && /INPUT|SELECT|TEXTAREA/.test(document.activeElement.tagName)) return;
      const y = window.scrollY;
      const scroll = panel.scrollLeft;
      panel.querySelectorAll('details[data-detail-key]').forEach(detail => { folds[detail.dataset.detailKey] = detail.open; });
      restoreFolds(replacement);
      panel.replaceWith(replacement); replacement.scrollLeft = scroll;
      enhanceTables(); globalStatus();
      window.scrollTo({top: y, behavior: 'instant'});
    },
  };
  enhanceTables(); accountPreview(); globalStatus();
})();

(() => {
  const status = document.getElementById('telegram-status');
  const button = document.getElementById('telegram-test');
  const labels = {pending: '대기', sent: '전송 완료', failed: '실패', expired: '만료', cancelled: '취소'};
  const categories = {system:'시작·종료', analysis:'분석', orders:'주문·체결', risk:'청산·제한',
    errors:'오류', review:'장 마감 복기', test:'연결 테스트'};
  async function refresh() {
    try {
      const response = await fetch('/api/telegram', {cache: 'no-store'});
      if (!response.ok) throw new Error('알림 상태 조회 실패');
      const data = await response.json();
      status.textContent = !data.available ? '알림 실행기 없음 · 프로그램 재시작 필요'
        : !data.configured ? '연결 정보 미등록 · Setup-Telegram.command 실행 필요'
        : data.enabled ? '알림 활성 · 대기 ' + (data.pending || 0) + '건' : '알림 비활성';
      if (data.error) status.textContent += ' · ' + data.error;
      button.disabled = !data.enabled;
      document.getElementById('telegram-recent').replaceChildren(...(data.recent || []).map(row => {
        const item = document.createElement('p');
        item.textContent = new Date(row.created * 1000).toLocaleString('ko-KR', {timeZone: 'Asia/Seoul'})
          + ' · ' + (categories[row.category] || row.category) + ' · ' + (labels[row.status] || row.status) + (row.error ? ' · ' + row.error : '');
        return item;
      }));
    } catch (error) { status.textContent = error.message; }
  }
  button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await fetch('/api/telegram/test', {method: 'POST',
        headers: {'Content-Type': 'application/json', 'X-Requested-With': 'TradingAgents'}, body: '{}'});
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || '테스트 요청 실패');
      await refresh();
      status.textContent += ' · 테스트 알림 전송 대기';
    } catch (error) { status.textContent = error.message; button.disabled = false; }
  });
  refresh();
  setInterval(refresh, 15000);
})();

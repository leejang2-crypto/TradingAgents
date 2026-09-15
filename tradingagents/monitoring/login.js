document.getElementById('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button');
  const message = document.getElementById('login-message');
  button.disabled = true;
  message.textContent = '로그인 중...';
  try {
    const response = await fetch('/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json', 'X-Requested-With': 'TradingAgents'},
      body: JSON.stringify(Object.fromEntries(new FormData(form)))
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || '로그인 실패');
    form.reset();
    location.replace('/');
  } catch (error) {
    message.textContent = error.message;
  } finally {
    button.disabled = false;
  }
});

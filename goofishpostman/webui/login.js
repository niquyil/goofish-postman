'use strict';

const form = document.getElementById('login-form');
const error = document.getElementById('login-error');

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  error.classList.add('hidden');
  const response = await fetch('/api/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ token: document.getElementById('token').value }),
  });
  const body = await response.json().catch(() => ({}));
  if (response.ok && body.ok) {
    window.location.href = '/';
  } else {
    error.textContent = body.error || '登录失败';
    error.classList.remove('hidden');
  }
});

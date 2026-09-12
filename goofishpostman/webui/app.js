'use strict';

// 首屏由 Jinja2 服务端渲染（#accounts / #messages / #events 带 data-rendered），
// 这里只负责后续的实时更新，避免刚打开页面就闪一下空白。
const serverRendered = {
  accounts: document.getElementById('accounts').hasAttribute('data-rendered'),
  messages: document.getElementById('messages').hasAttribute('data-rendered'),
  events: document.getElementById('events').hasAttribute('data-rendered'),
};

const STATUS_TEXT = {
  running: '监听中',
  starting: '连接中',
  stopped: '已停止',
  error: '异常',
};

const STATUS_CLASS = {
  running: 'pill-ok',
  starting: 'pill-warn',
  error: 'pill-err',
  stopped: 'pill-off',
};

let accounts = [];
let notifySettings = null;

// ── 工具 ────────────────────────────────────────────────────────────────────
async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  let body = {};
  try {
    body = await response.json();
  } catch {
    body = {};
  }
  if (response.status === 401 && !path.startsWith('/api/login')) {
    window.location.href = '/login';
    return { ok: false };
  }
  if (!response.ok || body.ok === false) {
    throw new Error(body.error || `请求失败 (${response.status})`);
  }
  return body;
}

let toastTimer = null;
function toast(message, isError = false) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.classList.toggle('error', isError);
  el.classList.remove('hidden');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add('hidden'), 3200);
}

function shortTime(iso) {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

// ── 账号列表 ────────────────────────────────────────────────────────────────
// 模板来自 Jinja2 渲染的 <template id="account-template">（服务端与客户端共用同一份标记）
function accountTemplate() {
  return document.getElementById('account-template').content.firstElementChild;
}

function renderAccounts() {
  const container = document.getElementById('accounts');
  container.replaceChildren();
  document.getElementById('account-count').textContent = String(accounts.length);

  if (accounts.length === 0) {
    const empty = document.createElement('p');
    empty.className = 'hint';
    empty.textContent = '还没有账号，点右上角「📱 扫码添加」或「+ 粘贴 Cookie」即可。';
    container.append(empty);
    return;
  }

  for (const account of accounts) {
    container.append(buildAccountRow(account));
  }
}

function buildAccountRow(account) {
  const node = accountTemplate().cloneNode(true);
  node.dataset.id = account.id;

  node.querySelector('.account-name').textContent = account.display_name;
  node.querySelector('.account-nickname').textContent = account.nickname
    ? `昵称：${account.nickname}`
    : '';

  const status = node.querySelector('.status');
  status.textContent = STATUS_TEXT[account.status] || account.status;
  status.classList.add(STATUS_CLASS[account.status] || 'pill-off');

  const meta = [];
  if (account.has_cookie) meta.push(`Cookie ${account.cookie_hint}`);
  meta.push(`收到 ${account.message_count} 条`);
  if (account.last_message_at) meta.push(`最后 ${shortTime(account.last_message_at)}`);
  if (account.retry_count) meta.push(`重连 ${account.retry_count} 次`);
  node.querySelector('.account-meta').textContent = meta.join(' · ');

  const problem = account.error
    || (account.missing_cookie_keys?.length ? `Cookie 缺少字段: ${account.missing_cookie_keys.join(', ')}` : '');
  if (problem) {
    const errorBox = node.querySelector('.account-error');
    errorBox.textContent = problem;
    errorBox.classList.remove('hidden');
  }

  const toggle = node.querySelector('.toggle');
  toggle.checked = account.enabled;
  toggle.addEventListener('change', () => {
    updateAccount(account.id, { enabled: toggle.checked }).catch((error) => {
      toggle.checked = !toggle.checked;
      toast(error.message, true);
    });
  });

  node.querySelector('.restart').addEventListener('click', () => {
    api(`/api/accounts/${account.id}/restart`, { method: 'POST' })
      .then(() => toast('已触发重连'))
      .catch((error) => toast(error.message, true));
  });

  node.querySelector('.edit').addEventListener('click', () => {
    const cookie = window.prompt(
      `粘贴 ${account.display_name} 的新 Cookie（留空取消）\n当前昵称：${account.nickname || '未知'}`,
    );
    if (cookie === null || !cookie.trim()) return;
    updateAccount(account.id, { cookie: cookie.trim() })
      .then(() => toast('Cookie 已更新，正在重连'))
      .catch((error) => toast(error.message, true));
  });

  node.querySelector('.reset-nickname').addEventListener('click', () => {
    api(`/api/accounts/${account.id}/reset-nickname`, { method: 'POST' })
      .then((body) => {
        if (body.account) upsertAccount(body.account);
        toast('昵称缓存已清空，等它下次发消息时会重新学习');
      })
      .catch((error) => toast(error.message, true));
  });

  node.querySelector('.remove').addEventListener('click', () => {
    if (!window.confirm(`确定删除账号「${account.display_name}」？`)) return;
    api(`/api/accounts/${account.id}`, { method: 'DELETE' })
      .then(() => {
        accounts = accounts.filter((item) => item.id !== account.id);
        renderAccounts();
        toast('账号已删除');
      })
      .catch((error) => toast(error.message, true));
  });

  return node;
}

function upsertAccount(account) {
  const index = accounts.findIndex((item) => item.id === account.id);
  if (index === -1) {
    accounts.push(account);
  } else {
    accounts[index] = account;
  }
  renderAccounts();
}

async function updateAccount(id, changes) {
  const body = await api(`/api/accounts/${id}`, { method: 'PATCH', body: JSON.stringify(changes) });
  upsertAccount(body.account);
}

// ── 消息流 / 事件 ───────────────────────────────────────────────────────────
// 条目结构统一由服务端的 <template> 提供，避免 JS 与实际标记走样。
// 注意：不能从列表里"借用"已有条目做模板 —— renderXxx 会先清空列表，
// 再逐条渲染，那样取到的模板是空的（曾因此导致消息流永远不显示内容）。
function cloneFrom(templateId) {
  const template = document.getElementById(templateId);
  if (template && template.content.firstElementChild) {
    return template.content.firstElementChild.cloneNode(true);
  }
  return document.createElement('li'); // 兜底：模板缺失时至少不报错
}

function fillSpans(node, values) {
  const spans = node.querySelectorAll('span');
  values.forEach((value, index) => {
    if (spans[index]) spans[index].textContent = value;
  });
  return node;
}

function renderMessages(messages) {
  const list = document.getElementById('messages');
  const count = document.getElementById('message-count');
  list.replaceChildren();
  count.textContent = String(messages.length);
  if (messages.length === 0) {
    const empty = document.createElement('li');
    empty.className = 'empty';
    empty.textContent = '暂无消息';
    list.append(empty);
    return;
  }
  for (const message of [...messages].reverse()) {
    list.append(buildMessageItem(message));
  }
}

function buildMessageItem(message) {
  return fillSpans(cloneFrom('message-template'), [
    message.at_text || shortTime(message.at),
    `${message.sender || message.send_user_name} → ${message.receiver || message.account_name}：${message.text}`,
  ]);
}

function renderEvents(events) {
  const list = document.getElementById('events');
  const count = document.getElementById('event-count');
  list.replaceChildren();
  count.textContent = String(events.length);
  if (events.length === 0) {
    const empty = document.createElement('li');
    empty.className = 'empty';
    empty.textContent = '暂无事件';
    list.append(empty);
    return;
  }
  for (const event of [...events].reverse()) {
    list.append(buildEventItem(event));
  }
}

function buildEventItem(event) {
  const node = cloneFrom('event-template');
  node.className = `level-${event.level}`;
  return fillSpans(node, [event.at_text || shortTime(event.at), event.message]);
}

// 增量更新的本地缓存（上限与后端保持一致）
let messages = [];
let events = [];

function pushMessage(message) {
  messages.push(message);
  if (messages.length > 200) messages = messages.slice(-200);
  renderMessages(messages);
}

function pushEvent(event) {
  events.push(event);
  if (events.length > 200) events = events.slice(-200);
  renderEvents(events);
}

// ── 飞书配置 ────────────────────────────────────────────────────────────────
function renderNotify() {
  if (!notifySettings) return;
  document.getElementById('notify-app-id').value = notifySettings.app_id || '';
  document.getElementById('notify-enabled').checked = Boolean(notifySettings.enabled);
  const state = document.getElementById('notify-state');
  if (notifySettings.configured && notifySettings.enabled) {
    state.textContent = '机器人应用';
    state.className = 'pill pill-ok';
  } else if (notifySettings.configured) {
    state.textContent = '已配置未启用';
    state.className = 'pill pill-warn';
  } else {
    state.textContent = '未配置';
    state.className = 'pill pill-off';
  }
}

function renderChatOptions(chats, selected) {
  const select = document.getElementById('notify-chat-id');
  select.replaceChildren();
  if (!chats.length) {
    const option = document.createElement('option');
    option.value = '';
    option.textContent = '（没有拿到群，先把机器人拉进群再试）';
    select.append(option);
    return;
  }
  for (const chat of chats) {
    const option = document.createElement('option');
    option.value = chat.chat_id;
    option.textContent = `${chat.name}（${chat.chat_id}）`;
    if (chat.chat_id === selected) option.selected = true;
    select.append(option);
  }
}

async function loadNotifyChats() {
  try {
    const body = await api('/api/notify/chats');
    renderChatOptions(body.chats || [], document.getElementById('notify-chat-id').value);
    toast(`拿到 ${(body.chats || []).length} 个群`);
  } catch (error) {
    toast(error.message, true);
  }
}

async function loadNotify() {
  const body = await api('/api/notify');
  notifySettings = {
    app_id: body.app_id,
    app_secret_set: body.app_secret_set,
    chat_id: body.chat_id,
    has_app_credentials: body.has_app_credentials,
    configured: body.configured,
    enabled: body.enabled,
  };
  renderNotify();
  renderChatOptions(body.chat_id ? [{ chat_id: body.chat_id, name: body.chat_id }] : [], body.chat_id);
  if (body.has_app_credentials) loadNotifyChats(); // 填了应用凭据就把群列表拉出来
}

// ── 数据加载与实时流 ────────────────────────────────────────────────────────
async function loadState() {
  const state = await api('/api/state');
  accounts = state.accounts;
  messages = state.messages;
  events = state.events;
  notifySettings = state.notify;

  // 首屏已经由服务端渲染，这里只补齐后续才会变化的部分，避免闪一下
  if (!serverRendered.accounts) renderAccounts();
  if (!serverRendered.messages) renderMessages(messages);
  if (!serverRendered.events) renderEvents(events);
  document.getElementById('account-count').textContent = String(accounts.length);
  document.getElementById('message-count').textContent = String(messages.length);
  document.getElementById('event-count').textContent = String(events.length);
  renderNotify();
}

function connectEvents() {
  const badge = document.getElementById('sse-state');
  const source = new EventSource('/api/events');

  source.onopen = () => {
    badge.textContent = '实时已连接';
    badge.className = 'pill pill-ok';
  };

  source.onerror = () => {
    badge.textContent = '连接断开，重连中…';
    badge.className = 'pill pill-err';
  };

  source.onmessage = (event) => {
    let payload;
    try {
      payload = JSON.parse(event.data);
    } catch {
      return;
    }
    if (payload.type === 'account') {
      upsertAccount(payload.account);
    } else if (payload.type === 'message') {
      pushMessage(payload.message);
    } else if (payload.type === 'event') {
      pushEvent(payload.event);
    }
  };
}

// ── 扫码登录 ────────────────────────────────────────────────────────────────
const QR_POLL_MS = 2500;

const qr = {
  session: null,
  timer: null,
  busy: false,
  done: false,
};

function qrElements() {
  return {
    modal: document.getElementById('qr-modal'),
    // 注意：这里没有 image —— 二维码是解码成功之后才由 showQrImage() 插进
    // qr-box 的，加载过程中 DOM 里根本不存在 <img>，也就不会有破图占位符
    box: document.getElementById('qr-box'),
    placeholder: document.getElementById('qr-placeholder'),
    status: document.getElementById('qr-status'),
    error: document.getElementById('qr-error'),
  };
}

function setQrStatus(text, className) {
  const { status } = qrElements();
  status.textContent = text;
  status.className = `pill ${className}`;
}

function setQrError(message) {
  const { error } = qrElements();
  error.textContent = message || '';
  error.classList.toggle('hidden', !message);
}

// 后端用 latin-1 传 PNG 字节，这里还原成二进制再拼 data URL
function pngDataUrl(text) {
  if (!text) return ''; // 没数据就别拼空 data URL，那会渲染成破图
  const bytes = new Uint8Array(text.length);
  for (let index = 0; index < text.length; index += 1) {
    bytes[index] = text.charCodeAt(index) & 0xff;
  }
  let binary = '';
  const chunk = 8192;
  for (let offset = 0; offset < bytes.length; offset += chunk) {
    binary += String.fromCharCode.apply(null, bytes.subarray(offset, offset + chunk));
  }
  return `data:image/png;base64,${btoa(binary)}`;
}

function qrImageFailed() {
  const { box, placeholder } = qrElements();
  box.replaceChildren(placeholder); // 丢掉失败的图；占位文案本身一直留在框里
  placeholder.hidden = false;
  placeholder.textContent = '二维码加载失败';
  setQrError('二维码图片加载失败，请点「刷新二维码」重试');
  setQrStatus('获取失败', 'pill-err');
}

// 图片解码成功之前不露出来：空 <img> / 未解码的 <img> 都会渲染成破图占位符。
// 注意：占位文案始终留在 .qr-box 里（只切 hidden），绝不能把它从 DOM 里摘掉 ——
// 摘掉之后 document.getElementById 返回 null，下次刷新就会往框里塞一个 "null" 文本节点。
function showQrImage(text) {
  const { box, placeholder } = qrElements();
  const src = pngDataUrl(text);
  if (!src) {
    qrImageFailed();
    return;
  }
  const image = document.createElement('img');
  image.className = 'qr-image';
  image.alt = '登录二维码';
  image.hidden = true;
  image.onload = () => {
    image.hidden = false;
    placeholder.hidden = true;
  };
  image.onerror = qrImageFailed;
  box.replaceChildren(placeholder, image);
  image.src = src;
}

function stopQrPolling() {
  if (qr.timer !== null) {
    clearTimeout(qr.timer);
    qr.timer = null;
  }
}

function resetQrView() {
  const { box, placeholder } = qrElements();
  box.replaceChildren(placeholder); // 去掉上一张二维码，占位文案留着
  placeholder.hidden = false;
  placeholder.textContent = '正在获取二维码…';
  setQrError('');
  setQrStatus('准备中', 'pill-warn');
}

async function startQrLogin() {
  if (qr.busy) return;
  qr.busy = true;
  qr.done = false;
  try {
    stopQrPolling();
    if (qr.session) {
      // 丢弃上一个未完成的会话
      api(`/api/qr/${qr.session}`, { method: 'DELETE' }).catch(() => {});
      qr.session = null;
    }
    resetQrView();

    const body = await api('/api/qr/start', { method: 'POST' });
    qr.session = body.session_id;
    showQrImage(body.image);
    setQrStatus(body.status_text || '请扫码', 'pill-warn');
    scheduleQrPoll(0);
  } catch (error) {
    setQrError(error.message);
    setQrStatus('获取失败', 'pill-err');
  } finally {
    // 放在 finally 里：中间任何一步抛错都要解锁，否则「刷新二维码」就再也点不动了
    qr.busy = false;
  }
}

function scheduleQrPoll(delay = QR_POLL_MS) {
  stopQrPolling();
  qr.timer = setTimeout(pollQrLogin, delay);
}

async function pollQrLogin() {
  if (!qr.session) return;
  try {
    const body = await api(`/api/qr/${qr.session}`);
    if (body.status === 'SCANNED') {
      setQrStatus(body.status_text, 'pill-warn');
    } else if (body.status === 'NEW') {
      setQrStatus(body.status_text, 'pill-warn');
    }
    if (body.confirmed && body.account) {
      stopQrPolling();
      qr.done = true;
      const session = qr.session;
      qr.session = null;
      api(`/api/qr/${session}`, { method: 'DELETE' }).catch(() => {});
      setQrStatus('登录成功', 'pill-ok');
      upsertAccount(body.account);
      toast(`账号「${body.account.display_name}」已添加并开始监听`);
      setTimeout(hideQrModal, 1200);
      return;
    }
  } catch (error) {
    // 404 表示二维码会话已失效，需要重新获取
    stopQrPolling();
    setQrError(error.message);
    setQrStatus('已失效', 'pill-err');
    return;
  }
  scheduleQrPoll();
}

function showQrModal() {
  qrElements().modal.classList.remove('hidden');
  startQrLogin();
}

function hideQrModal() {
  qrElements().modal.classList.add('hidden');
  stopQrPolling();
  if (qr.session && !qr.done) {
    api(`/api/qr/${qr.session}`, { method: 'DELETE' }).catch(() => {});
  }
  qr.session = null;
}

// ── 事件绑定 ────────────────────────────────────────────────────────────────
function bind() {
  document.getElementById('qr-open').addEventListener('click', showQrModal);
  document.getElementById('qr-close').addEventListener('click', hideQrModal);
  document.getElementById('qr-refresh').addEventListener('click', startQrLogin);
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') hideQrModal();
  });

  document.getElementById('refresh').addEventListener('click', () => {
    loadState().then(() => toast('已刷新')).catch((error) => toast(error.message, true));
  });

  const addForm = document.getElementById('add-form');
  document.getElementById('toggle-add').addEventListener('click', () => {
    addForm.classList.toggle('hidden');
  });
  document.getElementById('cancel-add').addEventListener('click', () => {
    addForm.classList.add('hidden');
  });

  addForm.addEventListener('submit', async (event) => {
    event.preventDefault();
    const name = document.getElementById('add-name').value.trim();
    const cookie = document.getElementById('add-cookie').value.trim();
    const enabled = document.getElementById('add-enabled').checked;
    if (!cookie) {
      toast('请粘贴 Cookie', true);
      return;
    }
    try {
      const body = await api('/api/accounts', {
        method: 'POST',
        body: JSON.stringify({ name, cookie, enabled }),
      });
      upsertAccount(body.account);
      addForm.reset();
      document.getElementById('add-enabled').checked = true;
      addForm.classList.add('hidden');
      toast('账号已添加');
    } catch (error) {
      toast(error.message, true);
    }
  });

  document.getElementById('notify-load-chats').addEventListener('click', loadNotifyChats);

  document.getElementById('notify-form').addEventListener('submit', async (event) => {
    event.preventDefault();
    const payload = {
      app_id: document.getElementById('notify-app-id').value.trim(),
      chat_id: document.getElementById('notify-chat-id').value.trim(),
      enabled: document.getElementById('notify-enabled').checked,
    };
    // 密钥留空表示"不改动"，避免每次保存都把已存的密钥清掉
    const appSecret = document.getElementById('notify-app-secret').value.trim();
    if (appSecret) payload.app_secret = appSecret;
    try {
      await api('/api/notify', { method: 'PUT', body: JSON.stringify(payload) });
      document.getElementById('notify-app-secret').value = '';
      await loadNotify();
      toast('飞书配置已保存');
    } catch (error) {
      toast(error.message, true);
    }
  });
}

async function main() {
  bind();
  try {
    await loadState();
  } catch (error) {
    toast(error.message, true);
  }
  connectEvents();
}

main();

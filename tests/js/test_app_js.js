'use strict';

/**
 * 用最小 DOM 桩在 Node 里执行 webui/app.js，验证渲染逻辑。
 *
 * 起因：消息流曾因为「renderMessages 先 replaceChildren 清空列表，再向列表
 * 借用第一条做模板」而永远渲染出空条目 —— 服务端首屏正常，但任何一次
 * JS 重绘都会把消息流清空。这类 bug 靠读代码很难发现，必须真的跑一遍 DOM。
 *
 * 用法：node tests/js/test_app_js.js
 * 不依赖任何第三方包（node:vm / node:assert 均为内置模块）。
 */

const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert');
const vm = require('node:vm');

// 默认跑仓库里的 app.js；APP_JS 可用来自查测试本身（例如塞一份改坏的副本，确认它会失败）
const APP_JS = process.env.APP_JS || path.join(__dirname, '..', '..', 'goofishpostman', 'webui', 'app.js');

// ── 最小 DOM 实现 ────────────────────────────────────────────────────────────
let classByTag = () => '';
// JS 现造出来的元素（例如二维码 <img>）在这里留一份，测试里要手动触发 onload
const created = [];

class FakeElement {
  constructor(tag = 'div') {
    this.tagName = tag.toUpperCase();
    this.children = [];
    this.parent = null;
    // classList 真实生效：只做空实现的话，"出错的行去掉 hidden"这类改动测不出来
    const classes = () => this._className.split(/\s+/).filter(Boolean);
    this.classList = {
      add: (...names) => {
        const current = classes();
        this._className = [...current, ...names.filter((n) => !current.includes(n))].join(' ');
      },
      remove: (...names) => {
        this._className = classes().filter((c) => !names.includes(c)).join(' ');
      },
      toggle: (name, force) => {
        const has = classes().includes(name);
        const wanted = force === undefined ? !has : Boolean(force);
        if (wanted) this.classList.add(name);
        else this.classList.remove(name);
      },
      contains: (name) => classes().includes(name),
    };
    this.dataset = {};
    this._text = '';
    this._className = classByTag(tag);
    this.hidden = false;
    this.checked = false;
    this.listeners = {};
    // 是否还挂在文档里。真实浏览器里被移除的元素，getElementById 就再也找不到了 ——
    // 少了这个标记，测试会漏掉"元素被 replaceChildren 摘掉后再也取不到"的 bug
    this.attached = false;
  }

  get className() {
    return this._className;
  }

  set className(value) {
    this._className = value;
  }

  get textContent() {
    if (this.children.length === 0) return this._text;
    return this.children.map((child) => child.textContent).join('');
  }

  set textContent(value) {
    this._text = String(value);
    this.children = [];
  }

  append(...nodes) {
    for (const node of nodes) {
      node.parent = this;
      markAttached(node);
      this.children.push(node);
    }
  }

  replaceChildren(...nodes) {
    for (const child of this.children) markDetached(child);
    this.children = [];
    this.append(...nodes);
  }

  removeAttribute(name) {
    // 浏览器里是把属性删掉（img 去掉 src 就不会去请求/渲染了），这里照做
    delete this[name];
  }

  hasAttribute() {
    return false;
  }

  cloneNode() {
    const copy = new FakeElement(this.tagName.toLowerCase());
    copy._className = this._className;
    copy.children = this.children.map((child) => {
      const childCopy = child.cloneNode();
      childCopy.parent = copy;
      return childCopy;
    });
    return copy;
  }

  querySelector(selector) {
    const found = this.querySelectorAll(selector);
    return found.length ? found[0] : null;
  }

  querySelectorAll(selector) {
    const wanted = selector.replace(/^\./, '');
    const tag = selector.startsWith('.') ? null : selector.toUpperCase();
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        const matchesClass = tag === null && child._className.split(/\s+/).includes(wanted);
        const matchesTag = tag !== null && child.tagName === tag;
        if (matchesClass || matchesTag) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }

  addEventListener(name, handler) {
    (this.listeners[name] ||= []).push(handler);
  }
}

function span(cls) {
  const node = new FakeElement('span');
  node.className = cls;
  return node;
}

function markAttached(node) {
  node.attached = true;
  for (const child of node.children) markAttached(child);
}

function markDetached(node) {
  node.attached = false;
  for (const child of node.children) markDetached(child);
}

function makeTemplate(id) {
  if (id === 'account-template') {
    // 结构对齐 templates/_macros.html 的 account_row
    const article = new FakeElement('article');
    article.className = 'account';
    const main = new FakeElement('div');
    main.className = 'account-main';
    const title = new FakeElement('div');
    title.className = 'account-title';
    const name = new FakeElement('strong');
    name.className = 'account-name';
    const nickname = span('account-nickname');
    title.append(name, nickname, span('pill status'));
    const meta = new FakeElement('div');
    meta.className = 'account-meta';
    const error = new FakeElement('div');
    error.className = 'account-error hidden';
    main.append(title, meta, error);

    const actions = new FakeElement('div');
    actions.className = 'account-actions';
    const toggle = new FakeElement('input');
    toggle.className = 'toggle';
    actions.append(
      toggle,
      Object.assign(span('ghost restart'), { tagName: 'BUTTON' }),
      Object.assign(span('ghost edit'), { tagName: 'BUTTON' }),
      Object.assign(span('ghost reset-nickname'), { tagName: 'BUTTON' }),
      Object.assign(span('danger remove'), { tagName: 'BUTTON' }),
    );

    article.append(main, actions);
    const template = new FakeElement('template');
    template.content = { firstElementChild: article };
    template.id = id;
    return template;
  }

  // 消息条目：<li><span class="time"></span><span class="text"></span></li>
  // 事件条目：同样是两个 span（级别放在 li 的 class 上）
  const li = new FakeElement('li');
  for (const cls of ['time', 'text']) li.append(span(cls));
  const template = new FakeElement('template');
  template.content = { firstElementChild: li };
  template.id = id;
  return template;
}

function createDocument(templateIds) {
  const registry = new Map();
  for (const id of templateIds) registry.set(id, makeTemplate(id));

  const ids = [
    'accounts', 'messages', 'events', 'account-count', 'message-count', 'event-count',
    'notify-uuid', 'notify-secret', 'notify-enabled', 'notify-state', 'notify-card',
    'add-form', 'add-name', 'add-cookie', 'add-enabled', 'toggle-add', 'cancel-add',
    'qr-open', 'qr-close', 'qr-refresh', 'qr-modal', 'qr-box', 'qr-placeholder',
    'qr-status', 'qr-error', 'sse-state', 'refresh', 'toast', 'login-form', 'login-error',
  ];
  for (const id of ids) {
    if (!registry.has(id)) registry.set(id, new FakeElement('div'));
  }
  // 这些是服务端渲染好的元素，一开始都挂在文档里
  for (const node of registry.values()) markAttached(node);

  return {
    registry,
    created,
    getElementById: (id) => {
      const node = registry.get(id) || null;
      // 从文档里移除过的元素，浏览器里就查不到了（这里保持一致）
      return node && node.attached ? node : null;
    },
    createElement: (tag) => {
      const node = new FakeElement(tag);
      created.push(node); // 测试里要拿到 JS 现造的 <img> 才能手动触发 onload
      return node;
    },
    addEventListener() {},
    querySelector: () => null,
    querySelectorAll: () => [],
  };
}

// ── 载入 app.js ──────────────────────────────────────────────────────────────
const document = createDocument(['account-template', 'message-template', 'event-template']);
const sandbox = {
  document,
  window: { location: { href: '/' }, confirm: () => true, prompt: () => null },
  console,
  setTimeout,
  clearTimeout,
  fetch: () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ ok: true }) }),
  EventSource: function EventSource() {
    this.addEventListener = () => {};
  },
  navigator: {},
  btoa: (value) => Buffer.from(value, 'binary').toString('base64'),
  Uint8Array,
  JSON,
  String,
  Number,
  Object,
  Array,
  Boolean,
  Error,
  Promise,
  Math,
  Date,
};
sandbox.window.document = document;
sandbox.globalThis = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP_JS, 'utf8'), sandbox, { filename: 'app.js' });

// ── 断言 ─────────────────────────────────────────────────────────────────────
const results = [];
function check(name, fn) {
  try {
    fn();
    results.push([name, true]);
  } catch (error) {
    results.push([name, false]);
    console.error(`\n✗ ${name}\n  ${error.message}`);
  }
}

const sample = {
  account_name: 'kisuke',
  send_user_name: '网课学习私人助理',
  sender: '网课学习私人助理',
  receiver: 'kisuke(13993122)',
  text: '在吗',
  at: '2026-09-12T00:40:00',
  at_text: '09-12 00:40:00',
};

check('buildMessageItem 渲染出时间/收发双方/正文', () => {
  const node = sandbox.buildMessageItem(sample);
  const text = node.textContent;
  assert.ok(text.includes('09-12 00:40:00'), `缺少时间: ${text}`);
  assert.ok(text.includes('网课学习私人助理 → kisuke(13993122)'), `缺少收发双方: ${text}`);
  assert.ok(text.includes('在吗'), `缺少正文: ${text}`);
});

check('buildMessageItem 不再用账号名做前缀', () => {
  const node = sandbox.buildMessageItem(sample);
  const spans = node.querySelectorAll('span');
  assert.strictEqual(spans.length, 2, `条目应只有时间与正文两个 span，实际 ${spans.length}`);
  assert.ok(!spans[0].textContent.includes('kisuke'), '第一个 span 应是时间，不该放账号名');
});

check('renderMessages 会先清空列表再渲染（回归：曾因借用列表首条做模板而渲染空条目）', () => {
  const list = document.getElementById('messages');
  // 预置一条"服务端首屏"的旧条目，模拟页面初始状态
  const old = sandbox.buildMessageItem({ ...sample, text: '旧消息' });
  list.replaceChildren(old);
  assert.ok(list.textContent.includes('旧消息'));

  sandbox.renderMessages([{ ...sample, text: '新消息' }]);

  const text = list.textContent;
  assert.ok(text.includes('新消息'), `渲染后缺少新消息，实际: ${JSON.stringify(text)}`);
  assert.ok(!text.includes('旧消息'), '旧条目应被清空');
  assert.ok(text.includes('→'), `条目缺少收发双方，实际: ${JSON.stringify(text)}`);
  assert.ok(text.includes('kisuke(13993122)'), `接收方应写成「昵称(账号)」，实际: ${JSON.stringify(text)}`);
  assert.strictEqual(document.getElementById('message-count').textContent, '1');
});

check('renderMessages 空列表显示占位', () => {
  sandbox.renderMessages([]);
  const list = document.getElementById('messages');
  assert.ok(list.textContent.includes('暂无消息'));
  assert.strictEqual(document.getElementById('message-count').textContent, '0');
});

check('renderMessages 多条按时间倒序（最新在前）', () => {
  sandbox.renderMessages([
    { ...sample, text: '第一条' },
    { ...sample, text: '第二条' },
  ]);
  const text = document.getElementById('messages').textContent;
  assert.ok(text.indexOf('第二条') < text.indexOf('第一条'), '最新的应排在最前');
});

check('buildEventItem 渲染时间与内容并带级别样式', () => {
  const node = sandbox.buildEventItem({ level: 'error', message: '飞书推送失败', at_text: '09-12 01:00:00' });
  assert.ok(node.textContent.includes('09-12 01:00:00'));
  assert.ok(node.textContent.includes('飞书推送失败'));
  assert.strictEqual(node.className, 'level-error');
});

check('renderEvents 渲染多条且不为空', () => {
  sandbox.renderEvents([
    { level: 'info', message: 'kisuke 已连接，开始监听', at_text: '09-12 00:30:00' },
    { level: 'warning', message: '连接已关闭', at_text: '09-12 00:31:00' },
  ]);
  const text = document.getElementById('events').textContent;
  assert.ok(text.includes('已连接'), `缺少事件内容: ${text}`);
  assert.strictEqual(document.getElementById('event-count').textContent, '2');
});

check('buildAccountRow 渲染账号名与状态', () => {
  const node = sandbox.buildAccountRow({
    id: 'a1',
    display_name: 'kisuke',
    nickname: '网课学习私人助理',
    enabled: true,
    status: 'running',
    has_cookie: true,
    cookie_hint: '***tk_1',
    missing_cookie_keys: [],
    message_count: 3,
    retry_count: 0,
    last_message_at: null,
  });
  const text = node.textContent;
  assert.ok(text.includes('kisuke'), `缺少账号名: ${text}`);
  assert.ok(text.includes('监听中'), `缺少状态: ${text}`);
});

check('buildAccountRow 显示昵称', () => {
  const node = sandbox.buildAccountRow({
    id: 'a1',
    display_name: 'kisuke',
    nickname: '网课学习私人助理',
    enabled: true,
    status: 'running',
    has_cookie: true,
    cookie_hint: '***x',
    missing_cookie_keys: [],
    message_count: 0,
    retry_count: 0,
    last_message_at: null,
  });
  assert.ok(node.textContent.includes('昵称：网课学习私人助理'), `缺少昵称展示: ${node.textContent}`);
});

check('buildAccountRow 无昵称时不显示占位', () => {
  const node = sandbox.buildAccountRow({
    id: 'a1',
    display_name: 'kisuke',
    nickname: '',
    enabled: true,
    status: 'stopped',
    has_cookie: true,
    cookie_hint: '***x',
    missing_cookie_keys: [],
    message_count: 0,
    retry_count: 0,
    last_message_at: null,
  });
  assert.ok(!node.textContent.includes('昵称：'), '无昵称时不该显示"昵称："');
});

check('buildAccountRow 有重置昵称按钮', () => {
  const node = sandbox.buildAccountRow({
    id: 'a1',
    display_name: 'kisuke',
    nickname: 'x',
    enabled: true,
    status: 'running',
    has_cookie: true,
    cookie_hint: '***x',
    missing_cookie_keys: [],
    message_count: 0,
    retry_count: 0,
    last_message_at: null,
  });
  assert.ok(node.querySelector('.reset-nickname'), '缺少重置昵称按钮');
});

// ── 账号列表重绘 ─────────────────────────────────────────────────────────────
// 回归："有数量、没账号"。renderAccounts 曾经先 replaceChildren 清空列表、再逐个
// 渲染条目；一旦某个账号带了 error（Cookie 失效时就是这样），渲染里写到模板中
// 不存在的 .account-error 就会抛异常 —— 列表空了，计数却还写着账号数。
const EXPIRED_ACCOUNT = {
  id: 'a-expired',
  display_name: 'kisuke',
  nickname: '网课学习私人助理',
  enabled: true,
  status: 'error',
  error: '登录态已失效（获取 token 失败，Cookie 可能已失效）：请在网页上重新扫码登录',
  has_cookie: true,
  cookie_hint: '***tk_1',
  missing_cookie_keys: [],
  message_count: 0,
  retry_count: 2,
  last_message_at: null,
};

function setAccounts(list) {
  vm.runInContext(`accounts = ${JSON.stringify(list)}`, sandbox);
}

check('renderAccounts 渲染出错的账号：条目和错误原因都在，列表不被清空', () => {
  setAccounts([EXPIRED_ACCOUNT]);
  sandbox.renderAccounts();

  const list = document.getElementById('accounts');
  assert.ok(list.textContent.includes('kisuke'), `列表空了，实际: ${JSON.stringify(list.textContent)}`);
  assert.ok(list.textContent.includes('登录态已失效'), '缺少错误原因');
  assert.strictEqual(document.getElementById('account-count').textContent, '1');
});

check('renderAccounts 一条出错不影响同列表里的其它账号', () => {
  setAccounts([EXPIRED_ACCOUNT, { ...EXPIRED_ACCOUNT, id: 'a-ok', display_name: '小号', status: 'running', error: '' }]);
  sandbox.renderAccounts();

  const text = document.getElementById('accounts').textContent;
  assert.ok(text.includes('kisuke') && text.includes('小号'), `两个账号都该渲染，实际: ${JSON.stringify(text)}`);
  assert.strictEqual(document.getElementById('account-count').textContent, '2');
});

check('出错账号的 .account-error 会去掉 hidden，正常账号保持隐藏', () => {
  setAccounts([EXPIRED_ACCOUNT, { ...EXPIRED_ACCOUNT, id: 'a-ok', error: '' }]);
  sandbox.renderAccounts();

  const rows = document.getElementById('accounts').querySelectorAll('article');
  assert.strictEqual(rows.length, 2);
  const errored = rows[0].querySelector('.account-error');
  const healthy = rows[1].querySelector('.account-error');
  assert.ok(errored.textContent.includes('登录态已失效'));
  assert.ok(!errored.classList.contains('hidden'), '出错的行不该还带 hidden');
  assert.ok(healthy.classList.contains('hidden'), '正常行应保持 hidden');
});

check('模板里缺 .account-error 时也只跳过错误行，不炸掉整份列表', () => {
  const template = document.registry.get('account-template');
  const article = template.content.firstElementChild;
  const main = article.querySelector('.account-main');
  const backup = main.children;
  main.children = backup.filter((child) => !child._className.includes('account-error'));
  try {
    setAccounts([EXPIRED_ACCOUNT]);
    sandbox.renderAccounts(); // 不应抛异常
    assert.ok(
      document.getElementById('accounts').textContent.includes('kisuke'),
      '缺错误行时账号本身还是要渲染出来',
    );
  } finally {
    main.children = backup;
  }
});

// ── 扫码弹窗 ─────────────────────────────────────────────────────────────────
// 破图占位符的根因：模板里那个空的 <img alt="登录二维码"> 会被浏览器渲染成
// 破图 + alt 文案（且 CSS 的 display 规则会压过 hidden 属性）。
// 现在改成：解码成功之前 DOM 里根本没有 <img>。
function lastCreatedImage() {
  return created.filter((node) => node.tagName === 'IMG').pop();
}

check('二维码没拿到之前：弹窗里没有 <img>，只显示等待文案', () => {
  sandbox.resetQrView();
  const box = document.getElementById('qr-box');
  assert.strictEqual(box.querySelectorAll('img').length, 0, '加载前不该存在 img 元素');
  const placeholder = document.getElementById('qr-placeholder');
  assert.strictEqual(placeholder.hidden, false, '应显示"正在获取二维码…"');
  assert.ok(placeholder.textContent.includes('正在获取二维码'), placeholder.textContent);
  assert.ok(box.textContent.includes('正在获取二维码'), '占位文案应在 .qr-box 里');
});

check('后端没给图片数据时：不插入 <img>，只报错', () => {
  sandbox.resetQrView();
  const box = document.getElementById('qr-box');
  const before = created.length;
  sandbox.showQrImage('');
  assert.strictEqual(created.length, before, '没有数据就不该造 img 元素');
  assert.strictEqual(box.querySelectorAll('img').length, 0);
  assert.ok(document.getElementById('qr-error').textContent.includes('二维码图片加载失败'));
  assert.strictEqual(document.getElementById('qr-status').textContent, '获取失败');
});

check('二维码要等解码成功才可见（中间不会露出占位符）', () => {
  sandbox.resetQrView();
  const box = document.getElementById('qr-box');
  const placeholder = document.getElementById('qr-placeholder');

  sandbox.showQrImage('\x89PNG\r\n'); // 任意非空字节即可，桩里不做真正解码
  const image = lastCreatedImage();
  assert.ok(image, '应创建一个 img 元素');
  assert.ok(String(image.src).startsWith('data:image/png;base64,'), '应设上 data URL');
  assert.strictEqual(image.hidden, true, '解码完成前图片必须隐藏，否则就是破图占位符');
  assert.strictEqual(placeholder.hidden, false, '此时应显示等待文案');

  image.onload();
  assert.strictEqual(image.hidden, false, '解码成功后图片才显示');
  assert.strictEqual(placeholder.hidden, true, '等待文案应藏起来');
  assert.strictEqual(box.querySelectorAll('img').length, 1, 'img 应挂在 .qr-box 里');
});

check('图片解码失败时不留 <img>，回到失败提示', () => {
  sandbox.resetQrView();
  const box = document.getElementById('qr-box');
  sandbox.showQrImage('\x89PNG');
  lastCreatedImage().onerror();
  assert.strictEqual(box.querySelectorAll('img').length, 0, '失败时 DOM 里不该留 img');
  assert.strictEqual(document.getElementById('qr-placeholder').hidden, false);
  assert.ok(box.textContent.includes('二维码加载失败'));
});

check('重新获取二维码会先把上一张图换掉', () => {
  sandbox.resetQrView();
  const box = document.getElementById('qr-box');
  sandbox.showQrImage('\x89PNG');
  lastCreatedImage().onload();
  assert.strictEqual(box.querySelectorAll('img').length, 1);

  sandbox.resetQrView();
  assert.strictEqual(box.querySelectorAll('img').length, 0, '重来时旧图应先移除');
  assert.ok(box.textContent.includes('正在获取二维码'));
});

check('点「刷新二维码」不会显示 null（回归：占位元素被摘掉后 getElementById 变 null）', () => {
  // 第一次打开：拿到二维码
  sandbox.resetQrView();
  sandbox.showQrImage('\x89PNG');
  lastCreatedImage().onload();
  const box = document.getElementById('qr-box');
  assert.strictEqual(box.querySelectorAll('img').length, 1, '第一张图应显示');
  assert.ok(
    document.getElementById('qr-placeholder'),
    '占位元素必须还在文档里 —— 摘掉之后 getElementById 会返回 null，刷新时就会渲染出 "null"',
  );

  // 用户点「刷新二维码」：resetQrView() + showQrImage() 再走一遍
  sandbox.resetQrView();
  assert.ok(
    box.textContent.includes('正在获取二维码'),
    `占位文案应回来，实际: ${JSON.stringify(box.textContent)}`,
  );
  assert.ok(!box.textContent.includes('null'), `框里不该出现 null，实际: ${JSON.stringify(box.textContent)}`);

  sandbox.showQrImage('\x89PNG');
  lastCreatedImage().onload();
  assert.strictEqual(box.querySelectorAll('img').length, 1, '第二张图也要能显示');
  assert.strictEqual(
    document.getElementById('qr-placeholder').hidden,
    true,
    '图片出来后等待文案应藏起来',
  );
});

// ── 输出 ─────────────────────────────────────────────────────────────────────
console.log('\napp.js 渲染测试:');
let failed = 0;
for (const [name, ok] of results) {
  console.log(`  ${ok ? '✓' : '✗'} ${name}`);
  if (!ok) failed += 1;
}
console.log(`\n${results.length - failed}/${results.length} 项通过`);
process.exit(failed ? 1 : 0);

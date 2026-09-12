"""Web 管理接口的测试：用 FastAPI TestClient，不监听真实端口。"""

from __future__ import annotations

from asyncio import run
from re import search

from fastapi.testclient import TestClient

from goofishpostman.path import BUILD_STAMP
from goofishpostman.store import Store
from goofishpostman.supervisor import _MAX_MESSAGES, MessageRecord, Supervisor
from goofishpostman.web import create_app

from helpers import GOOD_COOKIE, FakeLive, RecordingNotifier


class Harness:
    def __init__(self, client: TestClient, store: Store, supervisor: Supervisor) -> None:
        self.client = client
        self.store = store
        self.supervisor = supervisor


def build_harness(tmp_dir, token: str = '') -> Harness:
    store = Store(tmp_dir / 'accounts.json')
    if token:
        store.update_web(token=token)
    supervisor = Supervisor(store, RecordingNotifier())
    supervisor.live_factory = FakeLive
    client = TestClient(create_app(store, supervisor))
    return Harness(client, store, supervisor)


# ── 基本页面与状态 ────────────────────────────────────────────────────────────
def test_index_and_static_are_served(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    response = harness.client.get('/')
    assert response.status_code == 200
    assert '闲鱼消息汇总' in response.text

    css = harness.client.get('/static/style.css')
    assert css.status_code == 200
    assert '--accent' in css.text

    assert harness.client.get('/static/../store.py').status_code == 404


def test_hidden_attribute_beats_display_rules(tmp_dir) -> None:
    """hidden 属性必须能真的把元素藏起来（UA 样式那条会被 display 规则压掉）。"""
    harness = build_harness(tmp_dir)
    css = harness.client.get('/static/style.css').text
    rule = search(r'\[hidden\]\s*\{([^}]*)\}', css)
    assert rule is not None, '缺少 [hidden] 规则'
    assert 'display: none' in rule.group(1) and '!important' in rule.group(1)


def test_qr_dialog_has_no_img_before_it_loads(tmp_dir) -> None:
    """回归：扫码弹窗里那个空的 <img alt="登录二维码"> 就是破图占位符。

    现在图片由 JS 解码成功后才插进 DOM，服务端渲染的 HTML 里不该再有 <img>。
    """
    harness = build_harness(tmp_dir)
    page = harness.client.get('/').text
    qr_box = page.split('id="qr-box"', 1)[1].split('</div>', 1)[0]
    assert '<img' not in qr_box, f'二维码框里不该有 img: {qr_box}'
    assert '正在获取二维码' in qr_box


def test_static_assets_carry_build_stamp(tmp_dir) -> None:
    """静态资源要带构建版本查询串。

    不带的话浏览器会一直用缓存里的旧 css/js，服务重启了页面还是老样子
    （这次二维码占位符修不掉就是这个原因）。
    """
    harness = build_harness(tmp_dir)
    page = harness.client.get('/').text
    assert f'/static/style.css?v={BUILD_STAMP}' in page
    assert f'/static/app.js?v={BUILD_STAMP}' in page


def test_state_reports_empty_dashboard(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    body = harness.client.get('/api/state').json()
    assert body['ok'] is True
    assert (body['accounts'], body['messages'], body['events']) == ([], [], [])
    assert body['notify']['configured'] is False


def test_json_response_keeps_chinese_readable(tmp_dir) -> None:
    """中文不应被转义成 \\uXXXX，否则前端与调试都不好读。"""
    harness = build_harness(tmp_dir)
    account = harness.store.add(name='主力号', cookie=GOOD_COOKIE)
    # 运行态由 supervisor 同步，网页看到的状态来自它
    run(harness.supervisor.start_enabled())
    raw = harness.client.get('/api/state').text
    assert account.id in raw
    assert '主力号' in raw
    assert '\\u' not in raw


# ── 账号增删改 ────────────────────────────────────────────────────────────────
def test_create_account_starts_monitoring(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    response = harness.client.post('/api/accounts', json={'name': '主力号', 'cookie': GOOD_COOKIE, 'enabled': True})
    assert response.status_code == 201
    account = response.json()['account']
    assert account['display_name'] == '主力号'
    assert account['cookie_hint'] == '***bc_1'
    assert 'cookie' not in account

    # 已落盘，且监听任务已拉起（TestClient 的请求跑在内部事件循环里，
    # 这里只断言任务已创建，实际状态流转由 test_supervisor 覆盖）
    assert Store(harness.store.path).list_accounts()[0].cookie == GOOD_COOKIE
    runtime = harness.supervisor.runtimes[account['id']]
    assert runtime.task is not None


def test_create_account_requires_cookie(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    response = harness.client.post('/api/accounts', json={'name': 'x', 'cookie': '   '})
    assert response.status_code == 400
    assert 'cookie' in response.json()['error']
    assert harness.store.list_accounts() == []


def test_create_account_missing_field_is_422(tmp_dir) -> None:
    """FastAPI 的请求体校验直接拦住缺字段的请求。"""
    harness = build_harness(tmp_dir)
    assert harness.client.post('/api/accounts', json={'name': 'x'}).status_code == 422
    assert harness.store.list_accounts() == []


def test_update_account_name_and_toggle(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    account = harness.store.add(name='旧名', cookie=GOOD_COOKIE, enabled=False)

    response = harness.client.patch(f'/api/accounts/{account.id}', json={'name': '新名'})
    assert response.status_code == 200
    assert response.json()['account']['name'] == '新名'

    response = harness.client.patch(f'/api/accounts/{account.id}', json={'enabled': True})
    assert response.json()['account']['enabled'] is True

    response = harness.client.patch(f'/api/accounts/{account.id}', json={})
    assert response.status_code == 400


def test_update_missing_account_is_404(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    assert harness.client.patch('/api/accounts/nope', json={'name': 'x'}).status_code == 404
    assert harness.client.delete('/api/accounts/nope').status_code == 404
    assert harness.client.post('/api/accounts/nope/restart').status_code == 404


def test_delete_account_removes_runtime(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    account = harness.store.add(name='x', cookie=GOOD_COOKIE)
    assert harness.client.delete(f'/api/accounts/{account.id}').status_code == 200
    assert harness.store.list_accounts() == []
    assert account.id not in harness.supervisor.runtimes


def test_restart_account(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    account = harness.store.add(name='x', cookie=GOOD_COOKIE, enabled=True)
    assert harness.client.post(f'/api/accounts/{account.id}/restart').status_code == 200
    assert harness.supervisor.runtimes[account.id].task is not None


# ── 飞书配置 ──────────────────────────────────────────────────────────────────
def test_notify_roundtrip_never_returns_secret(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    assert harness.client.get('/api/notify').json() == {'ok': True, 'uuid': '', 'secret_set': False, 'enabled': True}

    response = harness.client.put('/api/notify', json={'uuid': 'uuid-1', 'secret': 'topsecret', 'enabled': True})
    assert response.status_code == 200

    body = harness.client.get('/api/notify').json()
    assert body['uuid'] == 'uuid-1'
    assert body['secret_set'] is True
    assert 'topsecret' not in str(body)
    assert harness.store.data.notify.secret == 'topsecret'
    # 推送器已按新配置重建
    assert harness.supervisor.notifier.uuid == 'uuid-1'


# ── 鉴权 ──────────────────────────────────────────────────────────────────────
def test_api_requires_token_when_configured(tmp_dir) -> None:
    harness = build_harness(tmp_dir, token='letmein')
    assert harness.client.get('/api/state').status_code == 401
    assert harness.client.post('/api/login', json={'token': 'wrong'}).status_code == 401

    good = harness.client.post('/api/login', json={'token': 'letmein'})
    assert good.status_code == 200
    # 登录后写入了会话 cookie，后续请求自动带上
    assert harness.client.get('/api/state').status_code == 200


def test_page_redirects_to_login_when_token_configured(tmp_dir) -> None:
    harness = build_harness(tmp_dir, token='letmein')
    response = harness.client.get('/', follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['location'] == '/login'

    login_page = harness.client.get('/login')
    assert login_page.status_code == 200
    assert '访问令牌' in login_page.text


def test_no_token_means_open_access(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    assert harness.client.get('/api/state').status_code == 200


# ── Jinja2 服务端渲染 ─────────────────────────────────────────────────────────
def test_index_renders_everything_server_side(tmp_dir) -> None:
    """首屏由 Jinja2 渲染：账号、消息、事件、飞书配置都应直接出现在 HTML 里。"""
    harness = build_harness(tmp_dir)
    account = harness.store.add(name='主力号', cookie=GOOD_COOKIE)
    harness.store.update_notify(uuid='uuid-1', secret='s3cret')
    harness.supervisor.sync_runtimes()

    runtime = harness.supervisor.runtimes[account.id]
    runtime.status = 'running'
    runtime.message_count = 3
    runtime.retry_count = 1
    harness.supervisor._append(
        harness.supervisor.messages,
        MessageRecord(account_id=account.id, account_name='主力号', send_user_name='买家', text='在吗'),
        _MAX_MESSAGES,
    )
    harness.supervisor.publish_event('info', account.id, '扫码登录成功')

    html = harness.client.get('/').text

    assert f'data-id="{account.id}"' in html
    assert '主力号' in html
    assert '监听中' in html  # 状态文案
    assert 'Cookie *' in html  # 脱敏提示
    assert '在吗' in html  # 消息
    assert '扫码登录成功' in html  # 事件
    assert 'value="uuid-1"' in html  # 飞书配置回填
    assert '已启用' in html
    assert '还没有账号' not in html  # 有账号就不显示空态
    assert '[[' not in html and ']]' not in html  # 定界符已全部解析


def test_index_marks_lists_as_server_rendered(tmp_dir) -> None:
    """app.js 靠 data-rendered 判断首屏是否已由服务端渲染，避免重复渲染。"""
    harness = build_harness(tmp_dir)
    html = harness.client.get('/').text
    assert html.count('data-rendered') == 3
    assert 'id="account-template"' in html


def test_index_escapes_user_content(tmp_dir) -> None:
    """账号名等用户输入必须转义，防止 XSS。"""
    harness = build_harness(tmp_dir)
    harness.store.add(name='<img src=x onerror=alert(1)>', cookie=GOOD_COOKIE)
    harness.supervisor.sync_runtimes()
    html = harness.client.get('/').text
    assert '<img src=x onerror' not in html
    assert '&lt;img src=x onerror=alert(1)&gt;' in html


def test_index_empty_state(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    html = harness.client.get('/').text
    assert '还没有账号' in html
    assert '暂无消息' in html
    assert '暂无事件' in html


def test_login_page_is_rendered_by_jinja(tmp_dir) -> None:
    harness = build_harness(tmp_dir)
    html = harness.client.get('/login').text
    assert '访问令牌' in html
    assert '/static/login.js' in html
    assert 'login-body' in html


# ── SSE ───────────────────────────────────────────────────────────────────────
def test_events_route_is_registered(tmp_dir) -> None:
    """SSE 是无限流：TestClient 会把整个响应读完从而挂住，所以这里只确认路由注册，
    真实推送由端到端脚本（uvicorn + httpx 只读首块）覆盖。"""
    harness = build_harness(tmp_dir)
    paths = {route.path for route in harness.client.app.routes}
    assert '/api/events' in paths

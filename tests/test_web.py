"""Web 管理接口的测试：用 FastAPI TestClient，不监听真实端口。"""

from __future__ import annotations

from asyncio import run
from datetime import UTC, datetime, timedelta
from re import search

from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from goofishpostman.accounts import FeishuNotifier
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
    supervisor = Supervisor(store=store, notifier=RecordingNotifier())
    supervisor.live_factory = FakeLive
    client = TestClient(create_app(store=store, supervisor=supervisor))
    return Harness(client=client, store=store, supervisor=supervisor)


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
    rule = search(pattern=r'\[hidden\]\s*\{([^}]*)\}', string=css)
    assert rule is not None, '缺少 [hidden] 规则'
    assert 'display: none' in rule.group(1) and '!important' in rule.group(1)


def test_qr_dialog_has_no_img_before_it_loads(tmp_dir) -> None:
    """回归：扫码弹窗里那个空的 <img alt="登录二维码"> 就是破图占位符。

    现在图片由 JS 解码成功后才插进 DOM，服务端渲染的 HTML 里不该再有 <img>。
    """
    harness = build_harness(tmp_dir)
    page = harness.client.get('/').text
    qr_box = page.split(sep='id="qr-box"', maxsplit=1)[1].split(sep='</div>', maxsplit=1)[0]
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
    assert harness.client.get('/api/notify').json() == {
        'ok': True,
        'app_id': '',
        'app_secret_set': False,
        'chat_id': '',
        'chat_name': '',
        'has_app_credentials': False,
        'configured': False,
        'enabled': True,
        'chats': [],
        'chats_fetched_at': '',
        'chats_hint': '群列表还没缓存：填好应用 ID 与密钥后点「获取群列表」',
    }

    response = harness.client.put(
        '/api/notify', json={'app_id': 'cli_1', 'app_secret': 'appsecret', 'chat_id': 'oc_1', 'enabled': True}
    )
    assert response.status_code == 200

    body = harness.client.get('/api/notify').json()
    assert body['app_id'] == 'cli_1'
    assert body['app_secret_set'] is True
    assert body['chat_id'] == 'oc_1'
    assert body['has_app_credentials'] is True  # 两个都填了才敢说能内嵌图片
    assert body['configured'] is True  # 再加上目标群才真的能发
    assert 'appsecret' not in str(body)  # 密钥永远不回传
    assert harness.store.data.notify.app_secret == 'appsecret'
    # 推送器已按新配置重建
    assert harness.supervisor.notifier.app_id == 'cli_1'
    assert harness.supervisor.notifier.can_send_via_app is True


def test_notify_chat_list_is_cached(tmp_dir, monkeypatch: MonkeyPatch) -> None:
    """「获取群列表」的结果要缓存：再次打开页面/刷新状态都用缓存，不再去调飞书接口。

    原来前端每次 loadNotify() 都会拉一次群列表（页面刷新就打一次），而且保存配置后
    下拉框只剩群 id（群名丢了，显示成「oc_xxx（oc_xxx）」）。
    """
    harness = build_harness(tmp_dir)
    harness.client.put('/api/notify', json={'app_id': 'cli_1', 'app_secret': 's3cret', 'chat_id': 'oc_1'})

    calls: list[str] = []

    async def fake_list_chats(self) -> list[dict[str, str]]:
        calls.append(self.app_id)
        return [{'chat_id': 'oc_1', 'name': '闲鱼消息汇总'}, {'chat_id': 'oc_2', 'name': ''}]

    monkeypatch.setattr(target=FeishuNotifier, name='list_chats', value=fake_list_chats)
    body = harness.client.get('/api/notify/chats').json()
    assert [chat['chat_id'] for chat in body['chats']] == ['oc_1', 'oc_2']
    # 群名列不出来（机器人不在群里/接口没给名字）时，不要显示成「oc_2（oc_2）」
    assert body['chats'][0]['label'] == '闲鱼消息汇总（oc_1）'
    assert body['chats'][1]['label'] == 'oc_2'
    assert body['chat_name'] == '闲鱼消息汇总'
    assert calls == ['cli_1']

    # 缓存落盘：/api/notify、/api/state、首屏 HTML 全都直接用缓存，不再调接口
    assert harness.store.data.notify.chats_fetched_at is not None
    assert harness.client.get('/api/notify').json()['chats'][0]['name'] == '闲鱼消息汇总'
    assert harness.client.get('/api/state').json()['notify']['chats'][1]['chat_id'] == 'oc_2'

    html = harness.client.get('/').text
    assert '<option value="oc_1" title="oc_1" selected>闲鱼消息汇总（oc_1）</option>' in html
    assert '群列表缓存于' in html
    assert calls == ['cli_1']  # 页面渲染不该再去调飞书


def test_notify_requires_app_credentials_for_chat_list(tmp_dir) -> None:
    """还没填应用凭据时「获取群列表」应直接提示，而不是发请求。"""
    harness = build_harness(tmp_dir)
    response = harness.client.get('/api/notify/chats')
    assert response.status_code == 400
    assert '应用' in response.json()['error']


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
    harness.store.update_notify(app_id='cli_1', app_secret='s3cret', chat_id='oc_1')
    harness.supervisor.sync_runtimes()

    runtime = harness.supervisor.runtimes[account.id]
    runtime.status = 'running'
    runtime.message_count = 3
    runtime.retry_count = 1
    harness.supervisor._append(
        buffer=harness.supervisor.messages,
        item=MessageRecord(account_id=account.id, account_name='主力号', send_user_name='买家', text='在吗'),
        limit=_MAX_MESSAGES,
    )
    harness.supervisor.publish_event(level='info', account_id=account.id, message='扫码登录成功')

    html = harness.client.get('/').text

    assert f'data-id="{account.id}"' in html
    assert '主力号' in html
    assert '监听中' in html  # 状态文案
    assert 'Cookie *' in html  # 脱敏提示
    assert '在吗' in html  # 消息
    assert '扫码登录成功' in html  # 事件
    assert 'value="cli_1"' in html  # 飞书配置回填
    assert '机器人应用' in html  # 配置齐全时的状态
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


def test_account_row_shows_the_login_life(tmp_dir) -> None:
    """账号卡片要显示登录态剩余时间：长连接同步来新的有效期后，一眼能看出还剩多久。"""
    harness = build_harness(tmp_dir)
    account = harness.store.add(name='主力号', cookie=GOOD_COOKIE)
    harness.supervisor.sync_runtimes()
    runtime = harness.supervisor.runtimes[account.id]
    runtime.status = 'running'
    runtime.token_expires_at = datetime.now(UTC) + timedelta(hours=1, minutes=20)

    html = harness.client.get('/').text
    assert '登录态剩余 1 小时' in html  # 服务端首屏
    state = harness.client.get('/api/state').json()
    assert state['accounts'][0]['token_life'].startswith('登录态剩余 1 小时')  # 前端刷新也拿得到


def test_account_actions_explain_themselves(tmp_dir) -> None:
    """「重连」这类按钮界面上没地方写解释，要靠 title 说明用途（不然没人知道什么时候按）。"""
    harness = build_harness(tmp_dir)
    harness.store.add(name='主力号', cookie=GOOD_COOKIE)
    harness.supervisor.sync_runtimes()

    html = harness.client.get('/').text
    assert '停掉当前长连接，立刻用现有 Cookie 重新连一次' in html
    assert '替换登录 Cookie' in html  # 已有的两个按钮提示也别丢


def test_account_error_box_is_always_rendered(tmp_dir) -> None:
    """回归：账号行模板里必须始终有 .account-error。

    JS 重绘时会克隆 <template id="account-template">，如果模板里缺了这一行，
    「出错的账号」（例如 Cookie 失效）渲染到一半就会抛异常 —— 列表已经被清空、
    计数还在，页面上就成了"有数量、没账号"。
    """
    harness = build_harness(tmp_dir)
    account = harness.store.add(name='过期号', cookie=GOOD_COOKIE)
    harness.supervisor.sync_runtimes()
    runtime = harness.supervisor.runtimes[account.id]
    runtime.status = 'error'
    runtime.error = '登录态已失效（获取 token 失败，Cookie 可能已失效）：请在网页上重新扫码登录'

    html = harness.client.get('/').text

    # 一份在服务端渲染的账号行、一份给 JS 用的模板，两处都要有这一行
    assert html.count('class="account-error') == 2
    assert '登录态已失效' in html
    template = html.split('id="account-template"')[1]
    assert 'account-error' in template.split('</template>')[0]


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

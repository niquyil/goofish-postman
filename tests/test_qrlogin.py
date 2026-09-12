"""扫码登录的测试。

闲鱼接口全部打桩，不发起网络请求；重点是状态机与「确认后自动建号」。
"""

from __future__ import annotations

from time import sleep
from unittest.mock import patch

from fastapi.testclient import TestClient
from pytest import fixture, mark
from requests.cookies import RequestsCookieJar

from goofishpostman import web as web_module
from goofishpostman.qrlogin import STATUS_CONFIRMED, STATUS_EXPIRED, STATUS_SCANNED, QrLoginError, QrSession
from goofishpostman.store import Store
from goofishpostman.supervisor import Supervisor
from goofishpostman.web import QR_SESSION_MAX_POLLS, QrLoginManager, create_app

from helpers import FakeLive, RecordingNotifier

COOKIE = 'unb=998877; tracknick=扫码号; _m_h5_tk=tk_9'


def make_state(status: str = 'PENDING') -> QrSession:
    return QrSession(
        session=object(),  # type: ignore[arg-type] - 所有接口都被打桩，不会真正使用
        device_id='DEV',
        cna='CNA',
        csrf_token='CSRF',
        status=status,
        qr_url='https://qr.example/abc',
        qr_t=1,
        qr_ck='ck',
    )


class StubQr:
    """按脚本推进的扫码桩：第一次轮询=已扫码，第二次=已确认。"""

    def __init__(self) -> None:
        self.create_calls = 0
        self.poll_calls = 0
        self.finish_calls = 0

    def create_login_session(self) -> QrSession:
        self.create_calls += 1
        return make_state()

    def start_qr_login(self, state: QrSession) -> QrSession:
        state.status = 'NEW'
        return state

    def poll_qr_login(self, state: QrSession) -> str:
        self.poll_calls += 1
        state.status = STATUS_SCANNED if self.poll_calls == 1 else STATUS_CONFIRMED
        return state.status

    def finish_qr_login(self, state: QrSession) -> dict:
        self.finish_calls += 1
        return {'unb': '998877', 'tracknick': '扫码号', 'cookie': COOKIE, 'device_id': 'DEV'}


class QrHarness:
    def __init__(self, client: TestClient, store: Store, supervisor: Supervisor, stub: StubQr) -> None:
        self.client = client
        self.store = store
        self.supervisor = supervisor
        self.stub = stub


@fixture
def qr(tmp_dir) -> QrHarness:
    """装配好 FastAPI 应用，并把扫码四步全部替换成桩。"""
    store = Store(tmp_dir / 'accounts.json')
    supervisor = Supervisor(store, RecordingNotifier())
    supervisor.live_factory = FakeLive
    stub = StubQr()
    client = TestClient(create_app(store, supervisor))
    with (
        patch.object(web_module, 'create_login_session', stub.create_login_session),
        patch.object(web_module, 'start_qr_login', stub.start_qr_login),
        patch.object(web_module, 'poll_qr_login', stub.poll_qr_login),
        patch.object(web_module, 'finish_qr_login', stub.finish_qr_login),
    ):
        yield QrHarness(client, store, supervisor, stub)


# ── 会话管理 ──────────────────────────────────────────────────────────────────
def test_manager_purges_expired_sessions() -> None:
    manager = QrLoginManager(ttl=0.05)
    session_id = manager.create(make_state())
    assert manager.get(session_id) is not None  # 未过期时可用

    sleep(0.06)
    assert manager.get(session_id) is None  # 超过 TTL 后被清理
    assert manager._sessions == {}


def test_manager_keeps_fresh_sessions() -> None:
    manager = QrLoginManager(ttl=999)
    session_id = manager.create(make_state())
    for _ in range(3):
        assert manager.get(session_id) is not None


def test_manager_drops_after_max_polls() -> None:
    manager = QrLoginManager(ttl=999)
    session_id = manager.create(make_state())
    for _ in range(QR_SESSION_MAX_POLLS + 1):
        manager.get(session_id)
    assert manager.get(session_id) is None


def test_manager_drop_and_clear() -> None:
    manager = QrLoginManager()
    first = manager.create(make_state())
    second = manager.create(make_state())
    manager.drop(first)
    assert manager.get(first) is None
    assert manager.get(second) is not None
    manager.clear()
    assert manager._sessions == {}


# ── 接口 ──────────────────────────────────────────────────────────────────────
def test_qr_start_returns_png_image(qr: QrHarness) -> None:
    response = qr.client.post('/api/qr/start')
    assert response.status_code == 200
    body = response.json()
    assert body['session_id']
    assert body['status'] == 'NEW'
    assert body['status_text']
    assert body['image'].startswith('\x89PNG')
    assert qr.stub.create_calls == 1


def test_qr_start_reports_upstream_failure(qr: QrHarness) -> None:
    def boom():
        raise QrLoginError('获取二维码失败: 连接超时')

    with patch.object(web_module, 'create_login_session', boom):
        response = qr.client.post('/api/qr/start')
    assert response.status_code == 502
    assert '连接超时' in response.json()['error']


def test_qr_poll_creates_account_and_starts_monitoring(qr: QrHarness) -> None:
    session_id = qr.client.post('/api/qr/start').json()['session_id']

    # 第一次：刚扫码
    body = qr.client.get(f'/api/qr/{session_id}').json()
    assert body['status'] == STATUS_SCANNED
    assert body['confirmed'] is False
    assert qr.store.list_accounts() == []

    # 第二次：已确认 -> 建号
    body = qr.client.get(f'/api/qr/{session_id}').json()
    assert body['status'] == STATUS_CONFIRMED
    assert body['confirmed'] is True
    account = body['account']
    assert account['display_name'] == '扫码号'
    assert account['has_cookie'] is True
    assert 'cookie' not in account

    stored = qr.store.list_accounts()[0]
    assert stored.cookie == COOKIE
    # TestClient 的请求跑在内部事件循环里，这里只断言任务已创建
    assert qr.supervisor.runtimes[stored.id].task is not None


def test_qr_poll_is_idempotent_after_confirm(qr: QrHarness) -> None:
    session_id = qr.client.post('/api/qr/start').json()['session_id']
    qr.client.get(f'/api/qr/{session_id}')
    first = qr.client.get(f'/api/qr/{session_id}').json()
    second = qr.client.get(f'/api/qr/{session_id}').json()

    assert first['account']['id'] == second['account']['id']
    assert len(qr.store.list_accounts()) == 1
    assert qr.stub.finish_calls == 1  # 不会重复完成登录


def test_qr_poll_unknown_session_is_404(qr: QrHarness) -> None:
    response = qr.client.get('/api/qr/does-not-exist')
    assert response.status_code == 404
    body = response.json()
    assert body['ok'] is False
    assert body['status'] == STATUS_EXPIRED


def test_qr_cancel_discards_session(qr: QrHarness) -> None:
    session_id = qr.client.post('/api/qr/start').json()['session_id']
    assert qr.client.delete(f'/api/qr/{session_id}').status_code == 200
    assert qr.client.get(f'/api/qr/{session_id}').status_code == 404
    manager = qr.client.app.state.web_app.qr_logins
    assert manager._sessions == {}


def test_qr_poll_reports_login_failure(qr: QrHarness) -> None:
    def boom(_state):
        raise QrLoginError('登录未完成，请重新扫码')

    with patch.object(web_module, 'finish_qr_login', boom):
        session_id = qr.client.post('/api/qr/start').json()['session_id']
        qr.client.get(f'/api/qr/{session_id}')  # 推进到 SCANNED
        response = qr.client.get(f'/api/qr/{session_id}')  # 触发完成登录并失败

    assert response.status_code == 502
    assert '重新扫码' in response.json()['error']


# ── 二维码渲染 ────────────────────────────────────────────────────────────────
def test_render_qr_png_is_valid_png() -> None:
    from goofishpostman.qrlogin import render_qr_png

    data = render_qr_png('https://passport.goofish.com/x?code=abc')
    assert data.startswith(b'\x89PNG\r\n\x1a\n')
    assert len(data) > 200


def test_qr_png_scales_with_box_size() -> None:
    from goofishpostman.qrlogin import render_qr_png

    small = render_qr_png('https://example.com/x', box_size=2)
    large = render_qr_png('https://example.com/x', box_size=8)
    assert len(large) > len(small)


# ── create_login_session 的真实调用（不联网） ─────────────────────────────────
class FakeUpstreamResponse:
    def __init__(self, payload: dict | None = None) -> None:
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


class FakeUpstreamSession:
    """替换 requests.Session，记录请求并按需返回 cookie。"""

    def __init__(self) -> None:
        self.cookies = RequestsCookieJar()
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url=None, **kwargs):
        self.calls.append(url)
        if 'mini_login' in (url or ''):
            # 模拟 passport 域下发 XSRF-TOKEN
            self.cookies.set('XSRF-TOKEN', 'csrf-token', domain='passport.goofish.com', path='/')
        if 'eg.js' in (url or ''):
            self.cookies.set('cna', 'cna-value', domain='.mmstat.com', path='/')
        return FakeUpstreamResponse()

    def post(self, url=None, **kwargs):
        self.calls.append(url)
        return FakeUpstreamResponse()


def test_create_login_session_works_without_user_id(monkeypatch) -> None:
    """回归：generate_device_id() 此前必填 user_id，这里不传参就 TypeError -> 接口 500。"""
    from goofishpostman import goofish_apis

    session = FakeUpstreamSession()
    monkeypatch.setattr(goofish_apis, 'Session', lambda: session)
    monkeypatch.setattr(goofish_apis, 'generate_tfstk', lambda timeout=15: '')

    state = goofish_apis.create_login_session()

    assert state.device_id  # 没有 user_id 也要能生成
    assert state.device_id.endswith('-')  # 无 user_id 时不留 'null' 尾巴
    assert 'null' not in state.device_id
    assert state.cna == 'cna-value'  # 从 mmstat 域兜底取到
    assert state.csrf_token == 'csrf-token'
    assert any('mini_login' in call for call in session.calls)


def test_generate_device_id_defaults_to_none() -> None:
    from re import fullmatch

    from goofishpostman.goofish_utils import generate_device_id

    # 形如 0387791C-BC62-4E7B-B0D6-57B10F5A3CE9-<user_id>
    pattern = r'[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}-?(.*)'
    assert fullmatch(pattern, generate_device_id()).group(1) == ''
    assert fullmatch(pattern, generate_device_id(None)).group(1) == ''
    assert fullmatch(pattern, generate_device_id('12345')).group(1) == '12345'
    assert 'null' not in generate_device_id(None)


# ── 未知异常不能变成裸 500 ────────────────────────────────────────────────────
def test_qr_start_unexpected_error_returns_502(qr: QrHarness) -> None:
    """任何非 QrLoginError 的意外都应该回可读的 502，而不是 500。"""

    def boom():
        raise TypeError('模拟未知错误')

    with patch.object(web_module, 'create_login_session', boom):
        response = qr.client.post('/api/qr/start')

    assert response.status_code == 502
    body = response.json()
    assert body['ok'] is False
    assert 'TypeError' in body['error'] or '模拟未知错误' in body['error']


def test_qr_status_unexpected_error_returns_502(qr: QrHarness) -> None:
    def boom(_state):
        raise ValueError('模拟轮询异常')

    with patch.object(web_module, 'poll_qr_login', boom):
        session_id = qr.client.post('/api/qr/start').json()['session_id']
        response = qr.client.get(f'/api/qr/{session_id}')

    assert response.status_code == 502
    assert response.json()['ok'] is False


def test_qr_png_render_failure_returns_502(qr: QrHarness) -> None:
    """二维码渲染失败（例如 qrcode 版本问题）也要给出可读错误。"""
    with patch.object(web_module, 'render_qr_png', lambda url: (_ for _ in ()).throw(ValueError('bad qr url'))):
        response = qr.client.post('/api/qr/start')

    assert response.status_code == 502
    assert 'bad qr url' in response.json()['error']


# ── goofish_apis 的命令行入口仍然可用 ──────────────────────────────────────────
def test_login_with_qrcode_is_callable() -> None:
    from goofishpostman import goofish_apis

    assert callable(goofish_apis.login_with_qrcode)
    assert callable(goofish_apis.create_login_session)


def test_build_session_cookie_string_filters_domains() -> None:
    from requests.cookies import RequestsCookieJar

    from goofishpostman.qrlogin import build_session_cookie_string

    jar = RequestsCookieJar()
    jar.set('unb', '1', domain='.goofish.com', path='/')
    jar.set('tracknick', '名字', domain='.goofish.com', path='/')
    jar.set('noise', 'x', domain='.example.com', path='/')
    text = build_session_cookie_string(type('S', (), {'cookies': jar})())
    assert text == 'unb=1; tracknick=名字'
    assert 'noise' not in text


@mark.parametrize('status', [STATUS_SCANNED, STATUS_CONFIRMED, STATUS_EXPIRED])
def test_status_text_covers_all_states(status: str) -> None:
    from goofishpostman.qrlogin import STATUS_TEXT

    assert STATUS_TEXT[status]

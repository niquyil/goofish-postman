"""Web 管理界面：FastAPI 应用 + SSE 实时推送。

路由一览：
    GET    /                    管理页面
    GET    /login               令牌登录页（仅在配置了 web.token 时需要）
    POST   /api/login           提交访问令牌，换取会话 cookie
    GET    /api/state           账号 / 消息 / 事件快照
    GET    /api/events          SSE 实时流
    POST   /api/accounts        新增账号
    PATCH  /api/accounts/{id}   改名字 / 开关 / 换 cookie
    DELETE /api/accounts/{id}   删除账号
    POST   /api/accounts/{id}/restart
    GET    /api/notify          飞书配置
    PUT    /api/notify          更新飞书配置
    POST   /api/qr/start        生成登录二维码（返回 PNG）
    GET    /api/qr/{id}         轮询扫码状态，确认后自动建号
    DELETE /api/qr/{id}         取消本次扫码
"""

from __future__ import annotations

from asyncio import Future, Queue, create_task, get_running_loop, sleep, wait_for
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from json import dumps, loads
from secrets import compare_digest, token_urlsafe
from signal import SIGINT, SIGTERM
from time import time
from typing import TYPE_CHECKING, Any

from anyio import to_thread
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from jinja2 import Environment, FileSystemLoader, select_autoescape
from loguru import logger
from pydantic import BaseModel
from uvicorn import Config, Server

from .accounts import FeishuNotifier, NotifyError
from .goofish_apis import create_login_session
from .path import BUILD_STAMP, STATIC_DIR, TEMPLATE_DIR
from .qrlogin import (
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    STATUS_TEXT,
    QrLoginError,
    QrSession,
    finish_qr_login,
    poll_qr_login,
    render_qr_png,
    start_qr_login,
)
from .store import Account, Store
from .supervisor import Supervisor, format_time

if TYPE_CHECKING:
    from collections.abc import Callable

SESSION_COOKIE = 'goofish_session'
# SSE 心跳间隔：防止中间层掐断空闲连接（也要短于常见客户端读超时）
SSE_KEEPALIVE = 10.0
# 不需要鉴权的路径
_PUBLIC_PATHS = ('/login', '/api/login', '/static/', '/favicon.ico')
# 一个二维码最多存活多久（秒），到期后会话被清理，需重新获取
QR_SESSION_TTL = 300.0
# 一次扫码会话最多轮询多少次，防止被无限调用
QR_SESSION_MAX_POLLS = 100


def build_templates() -> Jinja2Templates:
    """Jinja2 环境。

    变量定界符用 [[ ]]，避免和前端 JS 的模板字符串 / 对象字面量打架；
    语句定界符用 [% %]（Jinja 要求 block 与 variable 的起始定界符不同）。
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(('html', 'xml')),
        variable_start_string='[[',
        variable_end_string=']]',
        block_start_string='[%',
        block_end_string='%]',
        comment_start_string='[#',
        comment_end_string='#]',
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters['time'] = format_time
    # 模板里用它给静态资源加版本号（/static/app.js?v=...）：
    # 不然浏览器可能一直用缓存里的旧 css/js，重启服务也看不出变化。
    # 页面本身不显示它，只用来拼查询串。
    env.globals['build'] = BUILD_STAMP
    return Jinja2Templates(env=env)


TEMPLATES = build_templates()


class ChineseJSONResponse(JSONResponse):
    """默认的 JSONResponse 会把中文转成 \\uXXXX，这里保持可读。"""

    def render(self, content: Any) -> bytes:
        return dumps(content, ensure_ascii=False, allow_nan=False, separators=(',', ':')).encode('utf-8')


class ChineseJSONRoute(APIRoute):
    """FastAPI 自动序列化 handler 返回值时也走中文友好的编码。"""

    def get_route_handler(self) -> Callable:
        original = super().get_route_handler()

        async def handler(request: Request) -> Response:
            response = await original(request)
            body = getattr(response, 'body', None)
            if response.media_type == 'application/json' and body:
                return ChineseJSONResponse(
                    content=loads(body), status_code=response.status_code, headers=dict(response.headers)
                )
            return response

        return handler


# ── 请求体模型 ────────────────────────────────────────────────────────────────
class LoginBody(BaseModel):
    token: str = ''


class AccountCreate(BaseModel):
    name: str = ''
    cookie: str
    enabled: bool = True


class AccountPatch(BaseModel):
    name: str | None = None
    cookie: str | None = None
    enabled: bool | None = None


class NotifyBody(BaseModel):
    app_id: str | None = None
    app_secret: str | None = None
    chat_id: str | None = None
    enabled: bool | None = None


class QrLoginManager:
    """管理多次扫码登录会话（内存态，按 session_id 索引）。"""

    def __init__(self, ttl: float = QR_SESSION_TTL) -> None:
        self.ttl = ttl
        self._sessions: dict[str, QrSession] = {}
        self._polls: dict[str, int] = {}

    def _purge(self) -> None:
        now = time()
        for session_id, state in list(self._sessions.items()):
            if now - state.created_at > self.ttl or self._polls.get(session_id, 0) > QR_SESSION_MAX_POLLS:
                self.drop(session_id)

    def create(self, state: QrSession) -> str:
        self._purge()
        session_id = token_urlsafe(16)
        self._sessions[session_id] = state
        self._polls[session_id] = 0
        return session_id

    def get(self, session_id: str) -> QrSession | None:
        self._purge()
        state = self._sessions.get(session_id)
        if state is not None:
            self._polls[session_id] = self._polls.get(session_id, 0) + 1
        return state

    def drop(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._polls.pop(session_id, None)

    def clear(self) -> None:
        self._sessions.clear()
        self._polls.clear()


class WebApp:
    """把管理接口的路由处理器聚在一起；状态挂在实例上，便于测试替换。"""

    def __init__(self, store: Store, supervisor: Supervisor) -> None:
        self.store = store
        self.supervisor = supervisor
        # 每次启动一份会话令牌，避免把配置里的明文令牌写进 cookie
        self.sessions: set[str] = set()
        self.qr_logins = QrLoginManager()

    # ── 鉴权 ──────────────────────────────────────────────────────────────────
    @property
    def required_token(self) -> str:
        return self.store.data.web.token.strip()

    def is_authorized(self, request: Request) -> bool:
        if not self.required_token:
            return True
        return request.cookies.get(SESSION_COOKIE) in self.sessions

    # ── 路由注册 ──────────────────────────────────────────────────────────────
    def register(self, app: FastAPI) -> None:
        app.state.web_app = self
        app.middleware('http')(self.auth_middleware)
        for route_method, path, handler in (
            (app.get, '/', self.render_index),
            (app.get, '/login', self.render_login_page),
            (app.post, '/api/login', self.api_login),
            (app.get, '/api/state', self.api_get_state),
            (app.get, '/api/events', self.api_get_events),
            (app.post, '/api/accounts', self.api_create_account),
            (app.patch, '/api/accounts/{account_id}', self.api_update_account),
            (app.delete, '/api/accounts/{account_id}', self.api_delete_account),
            (app.post, '/api/accounts/{account_id}/restart', self.api_restart_account),
            (app.post, '/api/accounts/{account_id}/reset-nickname', self.api_reset_nickname),
            (app.get, '/api/notify', self.api_get_notify),
            (app.put, '/api/notify', self.api_update_notify),
            (app.get, '/api/notify/chats', self.api_list_notify_chats),
            (app.post, '/api/qr/start', self.api_start_qr_login),
            (app.get, '/api/qr/{session_id}', self.api_get_qr_status),
            (app.delete, '/api/qr/{session_id}', self.api_cancel_qr_login),
        ):
            route_method(path)(handler)

    async def auth_middleware(self, request: Request, call_next):
        if request.url.path.startswith(_PUBLIC_PATHS) or self.is_authorized(request):
            return await call_next(request)
        if request.url.path.startswith('/api/'):
            return ChineseJSONResponse({'ok': False, 'error': '未授权，请先登录'}, status_code=401)
        return RedirectResponse('/login', status_code=302)

    # ── 页面（Jinja2 服务端渲染，首屏数据直接进 HTML） ─────────────────────────
    async def render_index(self, request: Request) -> Response:
        self.supervisor.sync_runtimes()
        return TEMPLATES.TemplateResponse(
            request,
            'index.html',
            {
                # 供 <template> 使用的空占位（JS 克隆后自行填内容）
                'empty_item': {'at_text': '', 'level': 'info', 'sender': '', 'receiver': '', 'text': '', 'message': ''},
                'accounts': [runtime.to_template() for runtime in self.supervisor.runtimes.values()],
                'messages': [record.to_public() for record in reversed(self.supervisor.messages)],
                'events': [event.to_public() for event in reversed(self.supervisor.events)],
                'notify': self.store.data.notify.to_public(),
            },
        )

    async def render_login_page(self, request: Request) -> Response:
        return TEMPLATES.TemplateResponse(request, 'login.html', {})

    async def api_login(self, body: LoginBody) -> Response:
        if not self.required_token:
            return ChineseJSONResponse({'ok': True})
        if not compare_digest(body.token, self.required_token):
            await sleep(0.5)  # 轻微限速，增加暴力破解成本
            return ChineseJSONResponse({'ok': False, 'error': '访问令牌不正确'}, status_code=401)
        session = token_urlsafe(24)
        self.sessions.add(session)
        response = ChineseJSONResponse({'ok': True})
        response.set_cookie(SESSION_COOKIE, session, httponly=True, samesite='lax')
        return response

    # ── 状态与实时流 ──────────────────────────────────────────────────────────
    async def api_get_state(self) -> Response:
        return ChineseJSONResponse({'ok': True, **self.supervisor.build_snapshot()})

    async def api_get_events(self, request: Request) -> StreamingResponse:
        queue: Queue[dict] = Queue(maxsize=500)
        unsubscribe = self.supervisor.subscribe(queue.put_nowait)

        async def stream_events() -> AsyncIterator[bytes]:
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await wait_for(queue.get(), timeout=SSE_KEEPALIVE)
                        yield f'data: {dumps(payload, ensure_ascii=False)}\n\n'.encode()
                    except TimeoutError:
                        yield b': keepalive\n\n'
            finally:
                unsubscribe()

        return StreamingResponse(
            stream_events(),
            media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    # ── 账号 ──────────────────────────────────────────────────────────────────
    async def api_create_account(self, body: AccountCreate) -> Response:
        cookie = body.cookie.strip()
        if not cookie:
            return self._bad_request('cookie 不能为空')
        account = self.store.add(name=body.name.strip(), cookie=cookie, enabled=body.enabled)
        await self.supervisor.apply_enabled(account)
        self.supervisor.publish_event('info', account.id, f'新增账号 {account.display_name}')
        return ChineseJSONResponse({'ok': True, 'account': account.to_public()}, status_code=201)

    async def api_update_account(self, account_id: str, body: AccountPatch) -> Response:
        if self.store.get(account_id) is None:
            return self._not_found('账号不存在')

        changes: dict[str, Any] = {}
        if body.name is not None:
            changes['name'] = body.name
        if body.cookie is not None:
            cookie = body.cookie.strip()
            if not cookie:
                return self._bad_request('cookie 不能为空')
            changes['cookie'] = cookie
        if body.enabled is not None:
            changes['enabled'] = body.enabled
        if not changes:
            return self._bad_request('没有要更新的字段')

        account = self.store.update(account_id, **changes)
        # cookie 变了必须重连才会生效
        if 'cookie' in changes and account.enabled:
            await self.supervisor.restart(account_id)
        else:
            await self.supervisor.apply_enabled(account)
        return ChineseJSONResponse({'ok': True, 'account': account.to_public()})

    async def api_delete_account(self, account_id: str) -> Response:
        if self.store.get(account_id) is None:
            return self._not_found('账号不存在')
        await self.supervisor.stop(account_id)
        self.store.remove(account_id)
        self.supervisor.runtimes.pop(account_id, None)
        self.supervisor.publish_event('info', account_id, '账号已删除')
        return ChineseJSONResponse({'ok': True})

    async def api_restart_account(self, account_id: str) -> Response:
        if self.store.get(account_id) is None:
            return self._not_found('账号不存在')
        await self.supervisor.restart(account_id)
        return ChineseJSONResponse({'ok': True})

    async def api_reset_nickname(self, account_id: str) -> Response:
        """清掉本地缓存的昵称，下次收到该账号的消息时会重新学习。"""
        account = self.store.get(account_id)
        if account is None:
            return self._not_found('账号不存在')
        updated = self.store.clear_nickname(account_id) or account
        self.supervisor.publish_event('info', account_id, f'{updated.display_name} 昵称缓存已清空，将重新学习')
        return ChineseJSONResponse({'ok': True, 'account': updated.to_public()})

    # ── 飞书配置 ──────────────────────────────────────────────────────────────
    async def api_get_notify(self) -> Response:
        return ChineseJSONResponse({'ok': True, **self.store.data.notify.to_public()})

    async def api_list_notify_chats(self) -> Response:
        """用自建应用列出机器人所在的群（界面上挑一个当推送目标），并把结果缓存在本地。"""
        notify = self.store.data.notify
        if not notify.has_app_credentials:
            return ChineseJSONResponse({'ok': False, 'error': '先填应用 ID 与密钥'}, status_code=400)
        notifier = FeishuNotifier(app_id=notify.app_id, app_secret=notify.app_secret)
        try:
            chats = await notifier.list_chats()
        except NotifyError as e:
            return ChineseJSONResponse({'ok': False, 'error': str(e)}, status_code=502)
        finally:
            await notifier.close()
        # 缓存下来：页面重开、进程重启都不必再调一次飞书接口（界面上还能顺手显示缓存时间）
        cached = self.store.update_notify_chats(chats)
        return ChineseJSONResponse({'ok': True, **cached.to_public()})

    async def api_update_notify(self, body: NotifyBody) -> Response:
        changes = {key: value for key, value in body.model_dump().items() if value is not None}
        notify = self.store.update_notify(**changes)
        # 重建推送器，让新配置立即生效
        await self.supervisor.notifier.close()
        self.supervisor.notifier = FeishuNotifier(
            app_id=notify.app_id, app_secret=notify.app_secret, chat_id=notify.chat_id
        )
        self.supervisor.publish_event(
            'info', '', '飞书推送配置已更新' if notify.configured else '飞书推送配置已更新（还不完整，补齐后才会发送）'
        )
        return ChineseJSONResponse({'ok': True, **notify.to_public()})

    # ── 扫码登录 ──────────────────────────────────────────────────────────────
    async def api_start_qr_login(self) -> Response:
        """生成二维码；阻塞的 requests 调用放线程里跑，避免卡住事件循环。"""
        try:
            session = await to_thread.run_sync(create_login_session)
            qr_state = await to_thread.run_sync(start_qr_login, session)
            image = render_qr_png(qr_state.qr_url).decode('latin-1')
        except QrLoginError as e:
            logger.warning(f'生成二维码失败: {e}')
            return ChineseJSONResponse({'ok': False, 'error': str(e)}, status_code=502)
        except Exception as e:  # noqa: BLE001 - 意外错误也要回可读信息，而不是裸 500
            logger.exception(f'生成二维码时发生未预期错误: {e}')
            return ChineseJSONResponse({'ok': False, 'error': f'生成二维码失败: {e}'}, status_code=502)

        session_id = self.qr_logins.create(qr_state)
        return ChineseJSONResponse(
            {
                'ok': True,
                'session_id': session_id,
                'status': qr_state.status,
                'status_text': STATUS_TEXT.get(qr_state.status, qr_state.status),
                'image': image,
            }
        )

    async def api_get_qr_status(self, session_id: str) -> Response:
        qr_state = self.qr_logins.get(session_id)
        if qr_state is None:
            return ChineseJSONResponse(
                {'ok': False, 'error': '二维码已失效，请重新获取', 'status': STATUS_EXPIRED}, status_code=404
            )

        try:
            if qr_state.status not in (STATUS_CONFIRMED, STATUS_EXPIRED):
                await to_thread.run_sync(poll_qr_login, qr_state)
            account = await self._finish_qr(qr_state) if qr_state.status == STATUS_CONFIRMED else None
        except QrLoginError as e:
            self.qr_logins.drop(session_id)
            logger.warning(f'扫码登录失败: {e}')
            return ChineseJSONResponse({'ok': False, 'error': str(e)}, status_code=502)
        except Exception as e:  # noqa: BLE001 - 同上，避免扫码流程把 500 抛给前端
            self.qr_logins.drop(session_id)
            logger.exception(f'扫码状态查询发生未预期错误: {e}')
            return ChineseJSONResponse({'ok': False, 'error': f'扫码登录失败: {e}'}, status_code=502)

        body: dict[str, Any] = {
            'ok': True,
            'status': qr_state.status,
            'status_text': STATUS_TEXT.get(qr_state.status, qr_state.status),
            'confirmed': account is not None,
        }
        if account is not None:
            body['account'] = account.to_public()
        return ChineseJSONResponse(body)

    async def api_cancel_qr_login(self, session_id: str) -> Response:
        self.qr_logins.drop(session_id)
        return ChineseJSONResponse({'ok': True})

    # ── 辅助 ──────────────────────────────────────────────────────────────────
    @staticmethod
    def _bad_request(message: str) -> Response:
        return ChineseJSONResponse({'ok': False, 'error': message}, status_code=400)

    @staticmethod
    def _not_found(message: str) -> Response:
        return ChineseJSONResponse({'ok': False, 'error': message}, status_code=404)

    async def _finish_qr(self, qr_state: QrSession) -> Account | None:
        """登录已确认：建号并落盘；重复轮询返回同一个账号。"""
        if qr_state.account_id:
            return self.store.get(qr_state.account_id)
        info = await to_thread.run_sync(finish_qr_login, qr_state)
        account = self.store.add(name=info.get('tracknick') or info['unb'], cookie=info['cookie'], enabled=True)
        qr_state.account_id = account.id
        await self.supervisor.apply_enabled(account)
        self.supervisor.publish_event('info', account.id, f'扫码登录成功：{account.display_name}')
        logger.info(f'扫码登录成功：{account.display_name}')
        return account


def create_app(store: Store, supervisor: Supervisor) -> FastAPI:
    """组装 FastAPI 应用（方便测试，不关心谁来跑 ASGI）。"""
    web_app = WebApp(store, supervisor)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        logger.info(f'管理界面监听配置: http://{store.data.web.host}:{store.data.web.port}')
        logger.info(f'构建版本: {BUILD_STAMP}')
        try:
            yield
        finally:
            with suppress(Exception):
                await supervisor.stop_all()

    app = FastAPI(title='闲鱼消息汇总', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.router.route_class = ChineseJSONRoute
    app.mount('/static', StaticFiles(directory=str(STATIC_DIR)), name='static')
    web_app.register(app)
    return app


def console_url(host: str, port: int) -> str:
    """管理界面的访问地址。

    监听 `0.0.0.0` / `::` 时换成回环地址 —— 那是「监听所有网卡」的意思，
    浏览器打不开 0.0.0.0。
    """
    display = '127.0.0.1' if host in ('', '0.0.0.0', '::') else host
    return f'http://{display}:{port}'


async def serve(
    store: Store,
    supervisor: Supervisor,
    host: str = '',
    port: int | None = None,
    ready: Future | None = None,
    ephemeral_port: bool = False,
) -> None:
    """用 uvicorn 启动服务（阻塞直到进程被终止）。

    端口优先级：显式传入的 port > 配置里的 web.port；
    `ephemeral_port=True` 时交给系统分配空闲端口（测试用，避免撞上已运行的实例）。
    注意不能用 port=0 表达「随机端口」—— 0 是 falsy，会被当成「未指定」。
    """
    settings = store.data.web
    listen_host = host or settings.host
    listen_port = 0 if ephemeral_port else (settings.port if port is None else port)

    server = Server(
        Config(
            create_app(store, supervisor),
            host=listen_host,
            port=listen_port,
            log_config=None,  # 日志交给 loguru
            access_log=False,
            lifespan='on',
        )
    )

    # 收到 Ctrl+C / SIGTERM 时优雅退出（uvicorn 在非主线程时不会自己装 handler）
    loop = get_running_loop()
    for sig in (SIGINT, SIGTERM):
        with suppress(NotImplementedError, AttributeError, ValueError):
            loop.add_signal_handler(sig, lambda: setattr(server, 'should_exit', True))

    async def report_ready() -> None:
        """等 uvicorn 真正监听后再回报地址（随机端口时才知道实际值）。"""
        while not server.started and not server.should_exit:
            await sleep(0.02)
        actual = get_actual_port(server) or listen_port
        url = console_url(listen_host, actual)
        if listen_host in ('', '0.0.0.0', '::'):
            logger.info(f'管理界面已启动: {url}（监听 {listen_host or "0.0.0.0"}:{actual}）')
        else:
            logger.info(f'管理界面已启动: {url}')
        if ready is not None and not ready.done():
            ready.set_result(url)

    reporter = create_task(report_ready())
    try:
        await server.serve()
    finally:
        reporter.cancel()


def get_actual_port(server: Server) -> int:
    """从已启动的 uvicorn 里取出真实监听端口。"""
    for http_server in getattr(server, 'servers', []):
        for sock in getattr(http_server, 'sockets', []) or []:
            with suppress(OSError, AttributeError, IndexError):
                return sock.getsockname()[1]
    return 0

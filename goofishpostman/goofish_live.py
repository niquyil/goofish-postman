from asyncio import create_task, sleep, to_thread
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from json import dumps, loads
from time import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from loguru import logger
from websockets import connect

from .cookies import Cookies, drop_cookies
from .goofish_apis import Goofish
from .goofish_utils import (
    carries_message,
    describe_message_content,
    describe_message_records,
    describe_token_life,
    extract_message_content,
    extract_message_info,
    extract_session_title,
    extract_token_expiry,
    format_content_text,
    format_time,
    generate_device_id,
    generate_mid,
    generate_uuid,
    iter_payloads,
)
from .headers import CHANNEL_USER_AGENT, USER_AGENT
from .types import APP_KEY, Message, MessageInfo, TextMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from websockets import ClientConnection

DEFAULT_BASE_URL = 'wss://wss-goofish.dingtalk.com/'

# 长连接握手头（/reg 与拉历史消息共用）
_WS_HEADERS = {
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Accept-Language': 'zh-CN,zh;q=0.9',
    'Cache-Control': 'no-cache',
    'Connection': 'Upgrade',
    'Origin': 'https://www.goofish.com',
    'Pragma': 'no-cache',
    'User-Agent': USER_AGENT,
}

# 长连接响应里需要原样回带的 header
_ECHO_HEADERS = ('app-key', 'ua', 'dt')

_HEART_BEAT_INTERVAL = 15
_TOKEN_REFRESH_INTERVAL = 600
# 连续这么多次续期失败就主动断开重连（token 有效期约 2 小时，10 分钟一次 = 最多拖半小时）
_TOKEN_REFRESH_MAX_FAILURES = 3


class CookieExpiredError(RuntimeError):
    """登录态失效（换不到 token）——重连也没用，得重新登录或换 Cookie。

    单独一个类型是为了让上层把提示写清楚，而不是丢一句笼统的异常。
    """


def describe_token_failure(result: object) -> str:
    """把「换不到 token」的服务端返回翻成一句人话。

    实测 Cookie 失效时服务端回的是 `FAIL_SYS_SESSION_EXPIRED::Session过期`
    （见 README 的排查记录），所以取 `::` 后面的中文部分；拿不到 ret 时退化成通用说法。
    """
    ret = ''
    if isinstance(result, dict):
        values = result.get('ret') or []
        if values:
            ret = str(values[0]).strip()
    if not ret:
        return '服务端没有返回 accessToken'
    return ret.split(sep='::', maxsplit=1)[1].strip() if '::' in ret else ret


def build_ack(message: dict) -> dict:
    """长连接消息的统一 ACK。"""
    headers = message.get('headers', {})
    ack_headers = {'mid': headers.get('mid') or generate_mid(), 'sid': headers.get('sid') or ''}
    ack_headers.update({key: headers[key] for key in _ECHO_HEADERS if key in headers})
    return {'code': 200, 'headers': ack_headers}


def extract_message_text(info: MessageInfo) -> str:
    """可读文案：文本给原文，其它类型给标注 + 说明 + 链接。

    非文本消息在报文里只有一句很粗的提醒（reminderContent，如 `[图片]`），
    图片/视频/语音的地址、卡片的标题与描述都藏在正文 JSON 里；这里把它们取出来，
    网页消息流与飞书卡片用的是同一份文案。正文认不出来时退回那句提醒。
    """
    content = extract_message_content(info['raw'])
    described = describe_message_content(content) if content else None
    if described is not None:
        text = format_content_text(described)
        if text:
            return text
    return info['send_message'] or '[不支持的消息类型]'


class GoofishLive:
    """单个闲鱼账号的长连接：收私信、回私信、维持登录态。

    多账号场景请为每个账号各建一个实例（实例之间没有共享状态）。
    """

    def __init__(self, cookies_str: str, base_url: str = DEFAULT_BASE_URL) -> None:
        self.cookies_str = cookies_str
        self.base_url = base_url
        self.cookies = Cookies.from_str(cookies_str)
        self.myid = self.cookies['unb']
        self.username = self.cookies.get(name='tracknick', default='')
        self.device_id = generate_device_id(self.myid)
        self.goofish = Goofish(cookies=self.cookies, device_id=self.device_id)
        # 握手完成（/reg 成功）时回调，让上层能立刻把账号标成「监听中」（Supervisor 会赋值）。
        # 注意它只代表连接建立，不代表连接健康 —— 重连退避看 Supervisor 自己的 healthy。
        self.on_connected = None
        # 非消息帧里可能夹带会话信息（商品标题），有就回调上去（Supervisor 会赋值）
        self.on_session_info = None
        # 登录态变化（token 续期/轮换后的 cookie）时回调上去，参数是新的 cookie 串与 token 过期时间。
        # Supervisor 会把它写回配置文件 —— 这样断线重连或重启进程后用的还是同一个登录态，
        # 而不是当初粘贴进来、早就过期的旧 cookie（这是"跑久了就提示获取 token 失败"的根因）。
        self.on_session = None
        # 当前长连接（main 里赋值）：回消息时优先复用它，不必为新连接付一次握手
        self.websocket: ClientConnection | None = None
        # 正在等待响应的 /r/SyncStatus/getState 请求（见 dispatch_message）
        self._pending_sync_mid: str | None = None

    async def list_all_conversations(self, cid: str) -> list[MessageInfo]:
        """拉取与某个用户的历史聊天记录（按时间由旧到新）。"""
        async with self._connect() as websocket:
            send_mid = generate_mid()
            msg = {
                'lwp': '/r/MessageManager/listUserMessages',
                'headers': {'mid': send_mid},
                'body': [f'{cid}@goofish', False, 9007199254740991, 20, False],
            }
            user_message_models: list[MessageInfo] = []
            async for raw_message in websocket:
                message = loads(raw_message)
                await websocket.send(dumps(build_ack(message)))
                if message.get('lwp') == '/s/vulcan':
                    await websocket.send(dumps(msg))
                if message.get('headers', {}).get('mid') != send_mid:
                    continue

                body = message.get('body', {})
                for user_message in body.get('userMessageModels', []):
                    user_message_models.insert(0, extract_message_info(user_message))

                if body.get('hasMore') != 1:
                    return user_message_models

                next_cursor = body['nextCursor']
                logger.info(f'has more history messages, next cursor: {next_cursor}')
                send_mid = generate_mid()
                msg['headers']['mid'] = send_mid
                msg['body'][2] = next_cursor
                await websocket.send(dumps(msg))
            return user_message_models

    async def create_chat(self, websocket: ClientConnection, toid: str, item_id: str) -> None:
        msg = {
            'lwp': '/r/SingleChatConversation/create',
            'headers': {'mid': generate_mid()},
            'body': [
                {
                    'pairFirst': f'{toid}@goofish',
                    'pairSecond': f'{self.myid}@goofish',
                    'bizType': '1',
                    'extension': {'itemId': item_id},
                    'ctx': {'appVersion': '1.0', 'platform': 'web'},
                }
            ],
        }
        await websocket.send(dumps(msg))

    async def send_message(self, websocket: ClientConnection, cid: str, toid: str, message: Message) -> None:
        content_type, data = message.to_custom()
        msg = {
            'lwp': '/r/MessageSend/sendByReceiverScope',
            'headers': {'mid': generate_mid()},
            'body': [
                {
                    'uuid': generate_uuid(),
                    'cid': f'{cid}@goofish',
                    'conversationType': 1,
                    'content': {'contentType': 101, 'custom': {'type': content_type, 'data': data}},
                    'redPointPolicy': 0,
                    'extension': {'extJson': '{}'},
                    'ctx': {'appVersion': '1.0', 'platform': 'web'},
                    'mtags': {},
                    'msgReadStatusSetting': 1,
                },
                {'actualReceivers': [f'{toid}@goofish', f'{self.myid}@goofish']},
            ],
        }
        await websocket.send(dumps(msg))

    async def send_text(self, websocket: ClientConnection, cid: str, toid: str, text: str) -> None:
        await self.send_message(websocket=websocket, cid=cid, toid=toid, message=TextMessage(text=text))

    async def init(self, websocket: ClientConnection) -> None:
        # 本地 `_m_h5_tk` 已经过期时先丢掉：mtop 的套路是「没有 token → 服务端下发一个新的」，
        # 带着过期 token 去问反而只会得到 SESSION_EXPIRED（实测），丢掉才有可能自愈。
        self._drop_expired_token()
        # 换 token 失败＝登录态废了（实测服务端回 FAIL_SYS_SESSION_EXPIRED::Session过期），
        # 把服务端的原话带上去，网页和日志里才看得出到底是哪种失效
        result = self.goofish.get_token()
        token = (result.get('data') or {}).get('accessToken', '') if isinstance(result, dict) else ''
        if not token:
            raise CookieExpiredError(describe_token_failure(result))
        # 换 token 这一步服务端会顺带刷新 _m_h5_tk / _m_h5_tk_enc：及时回写，别只留在内存里
        self.report_session()
        await websocket.send(
            dumps(
                {
                    'lwp': '/reg',
                    'headers': {
                        'cache-header': 'app-key token ua wv',
                        'app-key': APP_KEY,
                        'token': token,
                        'ua': CHANNEL_USER_AGENT,
                        'dt': 'j',
                        'wv': 'im:3,au:3,sy:6',
                        'sync': '0,0;0;0;',
                        'did': self.device_id,
                        'mid': generate_mid(),
                    },
                }
            )
        )

        current_time = int(time() * 1000)
        await websocket.send(
            dumps(
                {
                    'lwp': '/r/SyncStatus/ackDiff',
                    'headers': {'mid': generate_mid()},
                    'body': [
                        {
                            'pipeline': 'sync',
                            'tooLong2Tag': 'PNM,1',
                            'channel': 'sync',
                            'topic': 'sync',
                            'highPts': 0,
                            'pts': current_time * 1000,
                            'seq': 0,
                            'timestamp': current_time,
                        }
                    ],
                }
            )
        )
        logger.info(f'[{self.username}] init')

    @staticmethod
    async def run_heart_beat(websocket: ClientConnection) -> None:
        while True:
            await websocket.send(dumps({'lwp': '/!', 'headers': {'mid': generate_mid()}}))
            await sleep(_HEART_BEAT_INTERVAL)

    async def run_token_refresh(self) -> None:
        """后台续期登录态（单事件循环里用 sleep 而非线程）。

        mtop token 的有效期实测只有 2 小时左右，所以每 10 分钟打一次续期接口；
        每次成功后都把轮换过的 cookie 交给上层落盘（见 `report_session`）。
        连续失败 `_TOKEN_REFRESH_MAX_FAILURES` 次就主动断开长连接：让 Supervisor 重连一次，
        重连时会先丢掉过期 token 重新申请（见 `_drop_expired_token`），比挂着一个换不到
        token 的连接强。
        """
        failures = 0
        while True:
            await sleep(_TOKEN_REFRESH_INTERVAL)
            try:
                result = await to_thread(self.goofish.refresh_token)
                ret = str((result.get('ret') or [''])[0]) if isinstance(result, dict) else ''
                if ret and not ret.startswith('SUCCESS'):
                    failures += 1
                    # 续期没成功往往就是登录态快过期了：早点留一行日志，别等断线才知道
                    logger.warning(f'[{self.username}] 刷新 token 未成功（{ret}，连续 {failures} 次）')
                    if failures >= _TOKEN_REFRESH_MAX_FAILURES:
                        await self._restart_connection(reason=f'连续 {failures} 次刷新 token 失败')
                    continue
                failures = 0
                self.report_session()
            except Exception as e:  # noqa: BLE001 - 一次失败不能让续期任务退出
                failures += 1
                logger.error(f'[{self.username}] 刷新 token 失败: {e}（连续 {failures} 次）')
                if failures >= _TOKEN_REFRESH_MAX_FAILURES:
                    await self._restart_connection(reason=f'连续 {failures} 次刷新 token 出错')

    async def _restart_connection(self, reason: str) -> None:
        """主动断开当前长连接：Supervisor 会按退避重连，并重新申请一次 token。"""
        websocket = self.websocket
        if websocket is None:
            return
        logger.warning(f'[{self.username}] {reason}，主动断开长连接以便重新申请 token')
        await websocket.close()

    def report_session(self) -> None:
        """把轮换后的 cookie 与 token 过期时间交给上层（Supervisor 负责落盘与展示）。"""
        cookie = str(self.goofish.cookies)
        expires_at = extract_token_expiry(cookie)
        logger.debug(f'[{self.username}] 登录态已续期：{describe_token_life(expires_at)}')
        if self.on_session is not None:
            self.on_session(cookie, expires_at)

    def _drop_expired_token(self) -> None:
        """本地 token 已过期就先删掉，让服务端在首次请求时重新下发一个。

        带着过期 token 去问，实测只会拿到 SESSION_EXPIRED（而且不会下发新 token）；
        按 mtop 的套路，没有 token 时服务端才会回一个新的。
        """
        token = self.goofish.session.cookies.get(name='_m_h5_tk')
        if not token:
            return
        expires_at = extract_token_expiry(f'_m_h5_tk={token}')
        if expires_at is not None and expires_at > datetime.now(UTC):
            return
        reason = f'已于 {format_time(expires_at)} 过期' if expires_at else '拿不到有效期'
        logger.info(f'[{self.username}] 本地 _m_h5_tk {reason}，丢掉旧 token 重新申请')
        drop_cookies(self.goofish.session.cookies, '_m_h5_tk', '_m_h5_tk_enc')

    def _build_ws_headers(self) -> dict[str, str]:
        host = urlsplit(self.base_url).netloc
        return _WS_HEADERS | {'Host': host, 'Cookie': str(self.cookies)}

    @asynccontextmanager
    async def _connect(self) -> AsyncIterator[ClientConnection]:
        """建立长连接并完成 /reg；init 失败会直接抛给调用方，退出时收尾后台任务。"""
        async with connect(uri=self.base_url, additional_headers=self._build_ws_headers()) as websocket:
            await self.init(websocket)
            if self.on_connected is not None:
                self.on_connected()
            tasks = [create_task(self.run_heart_beat(websocket)), create_task(self.run_token_refresh())]
            try:
                yield websocket
            finally:
                for task in tasks:
                    task.cancel()

    async def main(self) -> None:
        async with self._connect() as websocket:
            self.websocket = websocket
            try:
                async for raw_message in websocket:
                    message = loads(raw_message)
                    await websocket.send(dumps(build_ack(message)))
                    await self.dispatch_message(message=message, websocket=websocket)
            finally:
                self.websocket = None

    async def send_text_to_conversation(self, cid: str, toid: str, text: str) -> None:
        """往某个会话发一条文本（飞书里回复卡片时用）。

        优先复用正在跑的长连接；账号没在监听时临时开一条（和拉历史记录一样的做法）。
        """
        websocket = self.websocket
        if websocket is not None:
            await self.send_text(websocket=websocket, cid=cid, toid=toid, text=text)
            return
        async with self._connect() as temporary:
            await self.send_text(websocket=temporary, cid=cid, toid=toid, text=text)

    async def synchronize_state(self, websocket: ClientConnection) -> None:
        """主动取一次同步状态（服务端说"同步数据太长"时走这条）。

        真实客户端（闲鱼网页版内置的钉钉 IM SDK）的处理是：
        收到 syncExtraType.type 为 1/2 的帧 → 发 /r/SyncStatus/getState →
        拿到状态后再发一次 /r/SyncStatus/ackDiff 回执，之后服务端才会把积压的
        同步记录（真正的私信）补推下来。少了这两步，长连接只会收到会话/预热
        通知，永远等不到私信正文 —— 就是"消息收不到"。
        """
        mid = generate_mid()
        self._pending_sync_mid = mid
        await websocket.send(
            dumps({'lwp': '/r/SyncStatus/getState', 'headers': {'mid': mid}, 'body': [{'topic': 'sync'}]})
        )
        logger.debug(f'[{self.username}] 请求同步状态 /r/SyncStatus/getState (mid={mid})')

    async def acknowledge_state(self, websocket: ClientConnection, state: dict) -> None:
        """把服务端返回的同步状态回执过去，服务端才会补推积压记录。"""
        await websocket.send(
            dumps({'lwp': '/r/SyncStatus/ackDiff', 'headers': {'mid': generate_mid()}, 'body': [state]})
        )
        logger.debug(f'[{self.username}] 已回执同步状态 /r/SyncStatus/ackDiff')

    async def dispatch_message(self, message: dict, websocket: ClientConnection) -> None:
        """解析推送并交给 handle_message；解析失败只记日志，不断开连接。"""
        headers = message.get('headers')
        body = message.get('body')
        headers = headers if isinstance(headers, dict) else {}

        # 1) 等到的同步状态响应：回执给服务端（这一步之后它才会补推积压的私信记录）
        if self._pending_sync_mid and headers.get('mid') == self._pending_sync_mid:
            self._pending_sync_mid = None
            if isinstance(body, dict) and message.get('code') == 200:
                await self.acknowledge_state(websocket=websocket, state=body)
            else:
                logger.warning(f'[{self.username}] 同步状态响应异常: code={message.get("code")} body={str(body)[:200]}')
            return

        # 2) "同步数据太长"的协商帧：去取同步状态，本帧不带业务记录
        if isinstance(body, dict):
            extra = body.get('syncExtraType')
            if isinstance(extra, dict) and extra.get('type') in (1, 2):
                await self.synchronize_state(websocket)
                return

        try:
            info = extract_message_info(message)
        except Exception as e:  # noqa: BLE001 - 心跳/ACK 等非消息推送都会走到这里
            if carries_message(message):
                # 帧里确实带私信记录却没解析出来 = 报文形状变了（"消息收不到"就是这么来的）。
                # 只打记录本身：整帧前 300 字符永远是 headers，每条告警看起来都一样，没法排查。
                logger.warning(f'私信记录解析失败（{type(e).__name__}: {e}）: {describe_message_records(message)}')
            else:
                logger.debug(f'忽略非消息推送: {e}')
                await self.collect_session_info(message)
            return
        logger.info(f'{self.username} 收到来自 {info["send_user_name"]} 的信息: {extract_message_text(info)}')
        await self.handle_message(info, websocket)

    async def collect_session_info(self, message: dict) -> None:
        """从非消息帧里顺手捞「会话 → 商品标题」（会话/预热记录里才有）。

        一个会话下所有私信共用同一个会话 id，标题学到一次就能一直用；
        长连接起来后服务端会先推一批这类记录，所以不必额外请求。
        """
        if self.on_session_info is None:
            return
        for payload in iter_payloads(message):
            learned = extract_session_title(payload)
            if learned is not None:
                self.on_session_info(*learned)

    async def handle_message(self, message: MessageInfo, websocket: ClientConnection) -> None:
        """收到用户私信时的业务入口，接入 AI 回复逻辑请覆写此方法。"""

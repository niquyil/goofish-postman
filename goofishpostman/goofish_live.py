from asyncio import create_task, sleep, to_thread
from contextlib import asynccontextmanager
from json import dumps, loads
from time import time
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from loguru import logger
from websockets import connect

from .cookies import Cookies
from .goofish_apis import Goofish
from .goofish_utils import (
    carries_message,
    describe_message_records,
    extract_message_info,
    extract_session_title,
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


def build_ack(message: dict) -> dict:
    """长连接消息的统一 ACK。"""
    headers = message.get('headers', {})
    ack_headers = {'mid': headers.get('mid') or generate_mid(), 'sid': headers.get('sid') or ''}
    ack_headers.update({key: headers[key] for key in _ECHO_HEADERS if key in headers})
    return {'code': 200, 'headers': ack_headers}


def extract_message_text(info: MessageInfo) -> str:
    """从解析后的 payload 里取出可读文本（非文本消息给出类型占位）。

    推送里的消息体比历史记录多包一层（['1']['1'] vs ['1']），
    所以这里递归找 contentType/text 段，而不是写死路径。
    """
    custom = _find_custom(info['raw'])
    if custom:
        match custom.get('contentType'):
            case 1:
                text = (custom.get('text') or {}).get('text')
                if text:
                    return text
            case 3:
                return '[图片]'
    return info['send_message'] or '[不支持的消息类型]'


def _find_custom(value, depth: int = 0):
    """在 payload 里定位 {'contentType': .., 'text'/'image': ..} 这一段。"""
    if depth > 6:
        return None
    if isinstance(value, dict):
        if 'contentType' in value and ('text' in value or 'image' in value):
            return value
        for key in ('1', '6'):
            found = _find_custom(value.get(key), depth + 1)
            if found:
                return found
    return None


class GoofishLive:
    """单个闲鱼账号的长连接：收私信、回私信、维持登录态。

    多账号场景请为每个账号各建一个实例（实例之间没有共享状态）。
    """

    def __init__(self, cookies_str: str, base_url: str = DEFAULT_BASE_URL) -> None:
        self.cookies_str = cookies_str
        self.base_url = base_url
        self.cookies = Cookies.from_str(cookies_str)
        self.myid = self.cookies['unb']
        self.username = self.cookies.get('tracknick', '')
        self.device_id = generate_device_id(self.myid)
        self.goofish = Goofish(cookies=self.cookies, device_id=self.device_id)
        # 握手完成（/reg 成功）时回调，让上层能立刻把账号标成「监听中」（Supervisor 会赋值）。
        # 注意它只代表连接建立，不代表连接健康 —— 重连退避看 Supervisor 自己的 healthy。
        self.on_connected = None
        # 非消息帧里可能夹带会话信息（商品标题），有就回调上去（Supervisor 会赋值）
        self.on_session_info = None
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
        await self.send_message(websocket, cid, toid, TextMessage(text=text))

    async def init(self, websocket: ClientConnection) -> None:
        token = self.goofish.get_token().get('data', {}).get('accessToken', '')
        if not token:
            raise RuntimeError('获取 token 失败，请检查 cookie 是否已失效')
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

    @staticmethod
    async def run_token_refresh(goofish: Goofish) -> None:
        """后台续期登录态（单事件循环里用 sleep 而非线程）。"""
        while True:
            await sleep(_TOKEN_REFRESH_INTERVAL)
            try:
                await to_thread(goofish.refresh_token)
            except Exception as e:  # noqa: BLE001 - 一次失败不能让续期任务退出
                logger.error(f'刷新 token 失败: {e}')

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
            tasks = [create_task(self.run_heart_beat(websocket)), create_task(self.run_token_refresh(self.goofish))]
            try:
                yield websocket
            finally:
                for task in tasks:
                    task.cancel()

    async def main(self) -> None:
        async with self._connect() as websocket:
            async for raw_message in websocket:
                message = loads(raw_message)
                await websocket.send(dumps(build_ack(message)))
                await self.dispatch_message(message, websocket)

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
                await self.acknowledge_state(websocket, body)
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

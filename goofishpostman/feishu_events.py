"""在飞书里回复卡片 → 转成闲鱼私信。

飞书侧用官方 SDK 的**长连接**模式收事件（`im.message.receive_v1`），不需要公网回调地址。
长连接的 `start()` 是阻塞的、并且用 SDK 自己的事件循环，所以跑在一个守护线程里；
收到事件后把活儿交回主事件循环（uvicorn 那个），避免跨线程操作闲鱼连接。

用法：
    listener = FeishuReplyListener(app_id, app_secret, on_reply)
    listener.start()          # 在主事件循环里调用（会记住当前 loop）
"""

from __future__ import annotations

from asyncio import AbstractEventLoop, get_running_loop, run_coroutine_threadsafe
from json import JSONDecodeError, loads
from re import compile as compile_pattern
from threading import Thread
from typing import TYPE_CHECKING, NamedTuple

from loguru import logger

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

# 事件里 @ 人的占位符，例如 "你好 @_user_1"
_MENTION_PLACEHOLDER = compile_pattern(r'@_user_\d+\s*')
# 线程名，便于排查
_THREAD_NAME = 'feishu-ws'


class FeishuReply(NamedTuple):
    """一条可用的飞书回复。

    - target：被引用（回复）的那条卡片消息 id；空串表示只 @ 了机器人、没引用卡片
    - text：回复内容
    - message_id：用户那条消息自身的 id（用来回话）
    """

    target: str
    text: str
    message_id: str


def parse_incoming_message(event) -> FeishuReply | None:
    """从「接收消息」事件里取出回复目标与文本。

    不是文本消息、机器人自己发的、或者没有内容的，都返回 None。
    """
    message = getattr(getattr(event, 'event', None), 'message', None)
    if message is None:
        return None
    sender_type = getattr(getattr(event.event, 'sender', None), 'sender_type', '')
    if sender_type == 'bot':
        return None  # 别把机器人自己的消息当成用户回复（防回环）
    if (message.message_type or '') != 'text':
        logger.debug(f'飞书消息不是文本（{message.message_type}），先忽略')
        return None
    text = extract_text(message.content)
    if not text:
        return None
    return FeishuReply(
        target=message.parent_id or message.root_id or '', text=text, message_id=message.message_id or ''
    )


def extract_text(content: str | None) -> str:
    """把文本消息的 content（JSON 字符串）转成纯文本，去掉 @ 占位符。"""
    if not content:
        return ''
    try:
        parsed = loads(content)
    except JSONDecodeError:
        return ''
    text = parsed.get('text') if isinstance(parsed, dict) else None
    if not isinstance(text, str):
        return ''
    return _MENTION_PLACEHOLDER.sub('', text).strip()


class FeishuReplyListener:
    """长连接收飞书事件，把「回复卡片」交给主事件循环处理。"""

    def __init__(self, app_id: str, app_secret: str, on_reply: Callable[[str, str, str], Awaitable[None]]) -> None:
        self.app_id = app_id
        self.app_secret = app_secret
        # on_reply(被引用的卡片消息 id, 回复文本, 用户那条消息 id)
        self.on_reply = on_reply
        self.loop: AbstractEventLoop | None = None
        self._thread: Thread | None = None

    def start(self) -> None:
        """记住主事件循环并起后台线程跑长连接（未配凭据时什么都不做）。"""
        if not (self.app_id and self.app_secret):
            logger.debug('未配置飞书应用凭据，跳过回复监听')
            return
        self.loop = get_running_loop()
        self._thread = Thread(target=self._run, name=_THREAD_NAME, daemon=True)
        self._thread.start()
        logger.info('飞书回复监听已启动（长连接）')

    def _run(self) -> None:
        """后台线程：SDK 的长连接会一直阻塞在这里。"""
        from lark_oapi import EventDispatcherHandler, LogLevel, ws

        from .accounts import load_sdk  # 触发同一个惰性导入（约 10 秒，放在线程里做）

        load_sdk()
        handler = EventDispatcherHandler.builder('', '').register_p2_im_message_receive_v1(self._on_event).build()
        client = ws.Client(self.app_id, self.app_secret, event_handler=handler, log_level=LogLevel.ERROR)
        try:
            client.start()
        except Exception as e:  # noqa: BLE001 - 监听线程挂掉不能影响主服务
            logger.error(f'飞书回复监听退出: {type(e).__name__}: {e}')

    def _on_event(self, event) -> None:
        """SDK 的事件回调（跑在长连接线程里）：解析后交回主循环。"""
        incoming = parse_incoming_message(event)
        if incoming is None:
            return
        loop = self.loop
        if loop is None or loop.is_closed():
            logger.warning('主事件循环不可用，回复丢弃')
            return
        logger.info(f'收到飞书消息（引用 {incoming.target or "无"}）: {incoming.text!r}')
        run_coroutine_threadsafe(self.on_reply(*incoming), loop)

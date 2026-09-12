"""测试之间共用的假实现。

放在独立模块里，避免测试文件之间相对导入（pytest 默认 rootdir 导入模式不支持）。
"""

from __future__ import annotations

from asyncio import Event, get_running_loop, sleep
from typing import ClassVar

from goofishpostman.accounts import FeishuNotifier
from goofishpostman.goofish_live import CookieExpiredError

GOOD_COOKIE = 'unb=123456; tracknick=tester; _m_h5_tk=abc_1'
# 与真实报文里的接收方/昵称一致，贴近线上形状
RECEIVER_COOKIE = 'unb=13993122; tracknick=网课学习私人助理; _m_h5_tk=tk_1'

# raw['1']['6']['3'] 是闲鱼协议里的消息内容段（contentType=1 为文本）
MESSAGE = {
    'cid': 'CID',
    'send_user_id': 'U1',
    'send_user_name': '买家',
    'send_message': '在吗',
    'raw': {'1': {'6': {'3': {'contentType': 1, 'text': {'text': '在吗'}}}}},
}


class RecordingNotifier(FeishuNotifier):
    """不联网，只记录推送内容。

    私信走的是 send_card（富文本卡片），这里把它也记进 sent（拼成「标题\\n正文」，
    和原来的纯文本格式一致），cards 记「标题 + 正文」，card_payloads 还留一份
    明细、配色与图片地址（Supervisor 会给卡片附上时间/商品名与图片 url）。
    """

    def __init__(self, app_id: str = '', app_secret: str = '', chat_id: str = '') -> None:
        super().__init__(app_id=app_id, app_secret=app_secret, chat_id=chat_id)
        self.sent: list[str] = []
        self.cards: list[tuple[str, str]] = []
        self.card_payloads: list[dict] = []
        # 飞书里的回复（回复功能的反馈）
        self.replies: list[tuple[str, str]] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def send_card(
        self,
        title: str,
        content: str,
        details: dict[str, str] | None = None,
        color: str = '',
        images: tuple[str, ...] = (),
        media: dict | None = None,
    ) -> str:
        self.cards.append((title, content))
        self.card_payloads.append(
            {
                'title': title,
                'content': content,
                'details': details or {},
                'color': color,
                'images': list(images),
                'media': media,
            }
        )
        self.sent.append(f'{title}\n{content}')
        return f'om-{len(self.card_payloads)}'  # 假装是飞书返回的消息 id

    async def reply_message(self, message_id: str, text: str) -> None:
        self.replies.append((message_id, text))

    async def close(self) -> None:
        return None


class FakeLive:
    """假的长连接：建实例即视为连接成功。

    会调用 Supervisor 赋的 on_connected（真实实现里是 /reg 握手成功后回调），
    所以账号状态在连接建立时就会变成 running，不必等第一条消息。
    """

    instances: ClassVar[list[FakeLive]] = []

    def __init__(self, cookie: str, deliver_message: bool = True) -> None:
        self.cookie = cookie
        self.deliver_message = deliver_message
        self.handle_message = None
        self.on_connected = None
        FakeLive.instances.append(self)

    async def main(self) -> None:
        if self.on_connected is not None:
            self.on_connected()
        if self.deliver_message and self.handle_message is not None:
            await self.handle_message(MESSAGE, None)
        await Event().wait()


class ConnectOnlyLive:
    """只连上、不发任何消息：用来验证「连接建立即算监听中」。"""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self.handle_message = None
        self.on_connected = None

    async def main(self) -> None:
        if self.on_connected is not None:
            self.on_connected()
        await Event().wait()


class FailingLive:
    """连接即失败，用于验证重连与错误上报。"""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self.handle_message = None
        self.on_connected = None

    async def main(self) -> None:
        raise RuntimeError('boom')


class ExpiredCookieLive:
    """Cookie 过期：连接时抛 CookieExpiredError，用于验证提示文案。"""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self.handle_message = None
        self.on_connected = None

    async def main(self) -> None:
        raise CookieExpiredError('获取 token 失败，Cookie 可能已失效')


async def wait_status(supervisor, account_id: str, status: str, timeout: float = 2.0) -> None:
    deadline = get_running_loop().time() + timeout
    while get_running_loop().time() < deadline:
        if supervisor.runtimes[account_id].status == status:
            return
        await sleep(0.01)
    raise AssertionError(f'账号未进入 {status}')

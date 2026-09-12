from base64 import b64encode
from functools import cached_property
from hashlib import sha256
from hmac import new
from re import compile as compile_pattern
from time import time
from zlib import crc32

from httpx import AsyncClient
from loguru import logger

FEISHU_HEADERS = {'Content-Type': 'application/json'}
# 飞书自定义机器人的 webhook。这里是手写 HTTP 请求，没有用官方 lark-oapi，原因：
# 1. 那个 SDK 是 OpenAPI 应用 SDK，全包搜不到 bot/v2（自定义机器人）相关代码，
#    用它就必须改成自建应用（app_id/app_secret + 群 chat_id），飞书侧还要开权限、发版；
# 2. 它所有版本都要求 websockets<16，与闲鱼长连接的 websockets>=17 冲突。
WEBHOOK_TEMPLATE = 'https://open.feishu.cn/open-apis/bot/v2/hook/{uuid}'


def build_webhook_url(uuid: str) -> str:
    return WEBHOOK_TEMPLATE.format(uuid=uuid.strip())


def generate_feishu_sign(secret: str, timestamp: int) -> str:
    """飞书自定义机器人的签名：以 "{timestamp}\\n{secret}" 为 key 做 HMAC-SHA256 再 base64。"""
    hmac_code = new(key=f'{timestamp}\n{secret}'.encode(), digestmod=sha256).digest()
    return b64encode(hmac_code).decode('utf-8')


def build_text_payload(message: str, secret: str | None = None) -> dict:
    payload: dict = {'msg_type': 'text', 'content': {'text': message}}
    return _with_sign(payload, secret)


def wrap_code_block(content: str) -> str:
    """把内容包进 markdown 代码块（飞书会渲染成代码框）。

    内容里本来就带反引号时把围栏加长，避免提前闭合。
    """
    longest = run = 0
    for char in content:
        run = run + 1 if char == '`' else 0
        longest = max(longest, run)
    fence = '`' * max(3, longest + 1)
    return f'{fence}\n{content}\n{fence}'


# 卡片标题可用的配色（不同账号固定用不同颜色，扫一眼就知道是哪个号收到的）
HEADER_COLORS = ('blue', 'wathet', 'turquoise', 'green', 'yellow', 'orange', 'red', 'carmine', 'violet', 'indigo')
DEFAULT_HEADER_COLOR = 'blue'


def pick_header_color(seed: str) -> str:
    """按稳定哈希挑一个固定的标题配色（同一个账号每次都是同一个颜色）。"""
    if not seed:
        return DEFAULT_HEADER_COLOR
    return HEADER_COLORS[crc32(seed.encode()) % len(HEADER_COLORS)]


def format_details(details: dict[str, str]) -> str:
    """把明细拼成代码框里的多行文本（一行一个字段，取不到的字段不占行）。"""
    return '\n'.join(f'{key}：{value}' for key, value in details.items() if value)


# 正文里要变成可点链接的地址（http/https）。`fleamarket://` 这类 App 深链不转：
# 飞书不认这个协议，转成 markdown 链接会变成死链，原样留文本还能复制到手机上打开。
_URL_PATTERN = compile_pattern(r'https?://[^\s<>()\[\]"\']+')


def linkify_urls(text: str) -> str:
    """把正文里的 http(s) 地址变成 markdown 链接（飞书里可点开看大图/视频）。

    链接文字就用地址本身，所以渲染出来的样子和纯文本一致，只是多了一层可点性 ——
    网页消息流里同一份文案仍是纯文本，两边不会出现两种说法。
    """
    return _URL_PATTERN.sub(lambda match: f'[{match.group()}]({match.group()})', text)


def build_card_payload(
    title: str,
    content: str,
    details: dict[str, str] | None = None,
    secret: str | None = None,
    color: str = DEFAULT_HEADER_COLOR,
) -> dict:
    """富文本卡片：标题标明消息流向，明细放代码框，正文用普通文本。

    标题已经承担了"这是谁发给谁"的信息，正文不必再抢视觉（不进代码框），
    反而明细（时间/商品名）是辅助信息，放进代码框与正文自然分开。
    明细排在正文前面 —— 先看清是哪条消息，再读内容。

    用卡片 2.0 的 markdown 组件 —— 1.0 的 lark_md 不支持代码块。
    """
    elements: list[dict] = []
    line = format_details(details or {})
    if line:
        elements.append({'tag': 'markdown', 'content': wrap_code_block(line)})
    elements.append({'tag': 'markdown', 'content': linkify_urls(content)})
    payload: dict = {
        'msg_type': 'interactive',
        'card': {
            'schema': '2.0',
            'config': {'update_multi': True},
            'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': color},
            'body': {'direction': 'vertical', 'elements': elements},
        },
    }
    return _with_sign(payload, secret)


def _with_sign(payload: dict, secret: str | None) -> dict:
    if secret:
        timestamp = int(time())
        payload |= {'timestamp': str(timestamp), 'sign': generate_feishu_sign(secret, timestamp)}
    return payload


class Sender:
    """单条推送到飞书（命令行入口在用）。

    给了 title 就发富文本卡片（正文代码块），否则退回纯文本。
    """

    def __init__(self, uuid: str | None, secret: str | None = None) -> None:
        self.uuid = uuid
        self.secret = secret

    @cached_property
    def webhook(self) -> str:
        return build_webhook_url(self.uuid or '')

    def generate_sign(self, timestamp: int) -> str:
        return generate_feishu_sign(self.secret or '', timestamp)

    def build_payload(self, message: str) -> dict:
        return build_text_payload(message, self.secret)

    def build_card(self, title: str, content: str, details: dict[str, str] | None = None) -> dict:
        return build_card_payload(title, content, details, self.secret)

    async def send_message(self, message: str, title: str = '') -> None:
        payload = self.build_card(title, message) if title else self.build_payload(message)
        async with AsyncClient(headers=FEISHU_HEADERS, timeout=10.0) as client:
            response = await client.post(self.webhook, json=payload)
            logger.debug(response.json().get('msg'))

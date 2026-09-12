"""飞书推送的回归测试（不联网）。"""

from __future__ import annotations

from asyncio import run
from base64 import b64encode
from hashlib import sha256
from hmac import new
from typing import Self
from unittest.mock import patch

from goofishpostman.sender import (
    DEFAULT_HEADER_COLOR,
    FEISHU_HEADERS,
    HEADER_COLORS,
    Sender,
    build_card_payload,
    pick_header_color,
    wrap_code_block,
)


class FakeResponse:
    def json(self) -> dict:
        return {'code': 0, 'msg': 'success'}


class FakeClient:
    """记录 AsyncClient 的构造参数与 post 调用。"""

    last: FakeClient | None = None

    def __init__(self, **kwargs) -> None:
        self.init_kwargs = kwargs
        self.post_kwargs: dict = {}
        FakeClient.last = self

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *args) -> None:
        return None

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.post_kwargs = {'url': url, **kwargs}
        return FakeResponse()


def test_payload_without_secret() -> None:
    assert Sender(uuid='u').build_payload(message='hi') == {'msg_type': 'text', 'content': {'text': 'hi'}}


def test_payload_with_secret_is_signed() -> None:
    payload = Sender(uuid='u', secret='s').build_payload(message='hi')
    timestamp = int(payload['timestamp'])
    expected = b64encode(new(key=f'{timestamp}\ns'.encode(), digestmod=sha256).digest()).decode()
    assert payload['sign'] == expected


def test_send_message_uses_headers_and_payload() -> None:
    """回归：send_message 里引用的 headers 必须真实存在（曾被重构删掉过）。"""
    sender = Sender(uuid='uuid-1', secret='sec')

    with patch('goofishpostman.sender.AsyncClient', FakeClient):
        run(sender.send_message(message='你好'))

    client = FakeClient.last
    assert client is not None
    assert client.init_kwargs['headers'] == FEISHU_HEADERS
    assert client.post_kwargs['url'] == 'https://open.feishu.cn/open-apis/bot/v2/hook/uuid-1'
    body = client.post_kwargs['json']
    assert body['msg_type'] == 'text'
    assert body['content']['text'] == '你好'
    assert body['sign']  # 配了 secret 就必须带签名


def test_send_message_with_title_uses_card() -> None:
    """带 title 时发富文本卡片：标题写流向，正文是普通文本，签名照旧。"""
    sender = Sender(uuid='uuid-1', secret='sec')

    with patch('goofishpostman.sender.AsyncClient', FakeClient):
        run(sender.send_message(message='在吗', title='买家 → 主力号'))

    body = FakeClient.last.post_kwargs['json']
    assert body['msg_type'] == 'interactive'
    assert body['card']['header']['title']['content'] == '买家 → 主力号'
    assert body['card']['body']['elements'] == [{'tag': 'markdown', 'content': '在吗'}]
    assert body['sign']


def test_code_block_fence_grows_when_content_has_backticks() -> None:
    """内容里本身有反引号时，围栏要加长，避免提前闭合。"""
    assert wrap_code_block('看这个 ```python```') == '````\n看这个 ```python```\n````'
    assert wrap_code_block('普通内容') == '```\n普通内容\n```'


def test_card_details_go_into_a_code_block_above_the_content() -> None:
    """明细（时间/商品）放代码框且排在正文前面，正文自己不进代码框。"""
    payload = build_card_payload('买家 → 主力号', '在吗', details={'时间': '09-11 23:55', '商品': '玲娜贝儿钱包'})
    elements = payload['card']['body']['elements']
    assert elements[0]['content'] == '```\n时间：09-11 23:55\n商品：玲娜贝儿钱包\n```'
    assert elements[1] == {'tag': 'markdown', 'content': '在吗'}


def test_card_omits_empty_details() -> None:
    """取不到的字段不占行；全取不到时连代码框都不出现。

    新会话第一次收到消息时可能还没学到商品标题，这时只显示时间。
    """
    payload = build_card_payload('买家 → 主力号', '在吗', details={'时间': '', '商品': ''})
    assert payload['card']['body']['elements'] == [{'tag': 'markdown', 'content': '在吗'}]
    assert len(build_card_payload('买家 → 主力号', '在吗')['card']['body']['elements']) == 1

    only_item = build_card_payload('买家 → 主力号', '在吗', details={'时间': '', '商品': '玲娜贝儿钱包'})
    assert only_item['card']['body']['elements'][0]['content'] == '```\n商品：玲娜贝儿钱包\n```'


def test_header_color_is_stable_and_from_the_palette() -> None:
    """同一个账号每次都要同一个颜色，且颜色必须在飞书认得的调色板里。"""
    assert pick_header_color('acct-1') == pick_header_color('acct-1')
    assert pick_header_color('acct-1') in HEADER_COLORS
    assert pick_header_color('') == DEFAULT_HEADER_COLOR
    assert build_card_payload('t', 'c', color='green')['card']['header']['template'] == 'green'

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
    linkify_urls,
    pick_header_color,
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


def test_card_details_use_small_text_and_a_divider() -> None:
    """明细用小号灰字（notation）而不是代码框。

    代码框会被飞书多渲染一行「N 行代码」，那行没有意义；小字 + 分割线同样能把
    「消息属性」和「正文」分开，而且更干净。
    """
    payload = build_card_payload('买家 → 主力号', '在吗', details={'时间': '09-11 23:55', '商品': '玲娜贝儿钱包'})
    elements = payload['card']['body']['elements']
    assert elements[0] == {
        'tag': 'markdown',
        'content': '**时间** 09-11 23:55 ｜ **商品** 玲娜贝儿钱包',
        'text_size': 'notation',
    }
    assert elements[1] == {'tag': 'hr'}
    assert elements[2] == {'tag': 'markdown', 'content': '在吗'}


def test_card_omits_empty_details() -> None:
    """取不到的字段不占位；全取不到时连小字和分割线都不出现。

    新会话第一次收到消息时可能还没学到商品标题，这时只显示时间。
    """
    payload = build_card_payload('买家 → 主力号', '在吗', details={'时间': '', '商品': ''})
    assert payload['card']['body']['elements'] == [{'tag': 'markdown', 'content': '在吗'}]
    assert len(build_card_payload('买家 → 主力号', '在吗')['card']['body']['elements']) == 1

    only_item = build_card_payload('买家 → 主力号', '在吗', details={'时间': '', '商品': '玲娜贝儿钱包'})
    assert only_item['card']['body']['elements'][0]['content'] == '**商品** 玲娜贝儿钱包'


def test_header_color_is_stable_and_from_the_palette() -> None:
    """同一个账号每次都要同一个颜色，且颜色必须在飞书认得的调色板里。"""
    assert pick_header_color('acct-1') == pick_header_color('acct-1')
    assert pick_header_color('acct-1') in HEADER_COLORS
    assert pick_header_color('') == DEFAULT_HEADER_COLOR
    assert build_card_payload('t', 'c', color='green')['card']['header']['template'] == 'green'


def test_urls_in_the_body_become_clickable_links() -> None:
    """图片/视频消息的地址要能在飞书里点开（正文里的 http(s) 转成 markdown 链接）。"""
    url = 'https://img.alicdn.com/imgextra/i4/O1CN01RSAPMB1dNBgdIEGcB_!!53-xy_chat.heic'
    payload = build_card_payload('买家 → 主力号', f'[图片]\n{url}')
    assert payload['card']['body']['elements'][0]['content'] == f'[图片]\n[{url}]({url})'


def test_app_deep_links_are_left_as_plain_text() -> None:
    """`fleamarket://` 是闲鱼 App 的深链，飞书不认，转成链接只会变成死链。"""
    content = '去付款：fleamarket://order_detail?id=1&role=buyer'
    assert linkify_urls(content) == content


def test_linkify_keeps_surrounding_text() -> None:
    assert (
        linkify_urls('看这个 https://example.com/a.jpg 挺好')
        == '看这个 [https://example.com/a.jpg](https://example.com/a.jpg) 挺好'
    )
    assert linkify_urls('没有链接') == '没有链接'


def test_linkify_does_not_wrap_markdown_links_again() -> None:
    """已经是 markdown 链接/图片语法的部分不能再包一层。

    否则 `![图](url)` 会变成 `![图]([url](url))` —— 飞书会拿这个假 image_key 去
    校验图片并直接拒收整张卡片（实测 ErrCode 200570 invalid image keys）。
    """
    image = '![图片](https://example.com/a.jpg)'
    link = '[查看详情](https://example.com/b)'
    assert linkify_urls(image) == image
    assert linkify_urls(link) == link
    assert linkify_urls(f'{image} 和 {link}') == f'{image} 和 {link}'

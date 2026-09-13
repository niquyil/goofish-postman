"""飞书卡片组装的回归测试（不联网；发送逻辑在 test_store 里）。"""

from __future__ import annotations

from goofishpostman.sender import (
    DEFAULT_HEADER_COLOR,
    HEADER_COLORS,
    build_card,
    build_text_message,
    linkify_urls,
    pick_header_color,
)


def test_card_details_use_small_text_and_a_divider() -> None:
    """明细用小号灰字（notation）而不是代码框。

    代码框会被飞书多渲染一行「N 行代码」，那行没有意义；小字 + 分割线同样能把
    「消息属性」和「正文」分开，而且更干净。
    """
    card = build_card(title='买家 → 主力号', content='在吗', details={'时间': '09-11 23:55', '商品': '玲娜贝儿钱包'})
    elements = card['body']['elements']
    assert elements[0] == {
        'tag': 'markdown',
        'content': '**时间** 09-11 23:55\n**商品** 玲娜贝儿钱包',
        'text_size': 'notation',
    }
    assert elements[1] == {'tag': 'hr'}
    assert elements[2] == {'tag': 'markdown', 'content': '在吗'}


def test_card_omits_empty_details() -> None:
    """取不到的字段不占位；全取不到时连小字和分割线都不出现。

    新会话第一次收到消息时可能还没学到商品标题，这时只显示时间。
    """
    card = build_card(title='买家 → 主力号', content='在吗', details={'时间': '', '商品': ''})
    assert card['body']['elements'] == [{'tag': 'markdown', 'content': '在吗'}]
    assert len(build_card(title='买家 → 主力号', content='在吗')['body']['elements']) == 1

    only_item = build_card(title='买家 → 主力号', content='在吗', details={'时间': '', '商品': '玲娜贝儿钱包'})
    assert only_item['body']['elements'][0]['content'] == '**商品** 玲娜贝儿钱包'


def test_card_is_a_schema_2_card_with_the_title_as_header() -> None:
    card = build_card(title='买家 → 主力号', content='在吗')
    assert card['schema'] == '2.0'
    assert card['header']['title'] == {'tag': 'plain_text', 'content': '买家 → 主力号'}
    assert card['config'] == {'update_multi': True}


def test_text_message_shape() -> None:
    assert build_text_message('出问题了') == {'text': '出问题了'}


def test_header_color_is_stable_and_from_the_palette() -> None:
    """同一个账号每次都要同一个颜色，且颜色必须在飞书认得的调色板里。"""
    assert pick_header_color('acct-1') == pick_header_color('acct-1')
    assert pick_header_color('acct-1') in HEADER_COLORS
    assert pick_header_color('') == DEFAULT_HEADER_COLOR
    assert build_card(title='t', content='c', color='green')['header']['template'] == 'green'


def test_urls_in_the_body_become_clickable_links() -> None:
    """图片/视频消息的地址要能在飞书里点开（正文里的 http(s) 转成 markdown 链接）。"""
    url = 'https://img.alicdn.com/imgextra/i4/O1CN01RSAPMB1dNBgdIEGcB_!!53-xy_chat.heic'
    card = build_card(title='买家 → 主力号', content=f'[图片]\n{url}')
    assert card['body']['elements'][0]['content'] == f'[图片]\n[{url}]({url})'


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

"""飞书卡片的组装。

发送走官方 SDK（见 accounts.FeishuNotifier）：`content` 必须是 JSON 字符串，
鉴权与请求都由 SDK 负责，所以这里只负责把卡片对象拼出来。
"""

from __future__ import annotations

from json import dumps
from re import compile as compile_pattern
from zlib import crc32


def content_json(content: dict) -> str:
    """消息接口的 `content` 字段：JSON 字符串（卡片对象、文本对象都走它）。"""
    return dumps(content, ensure_ascii=False)


# 卡片标题可用的配色（不同账号固定用不同颜色，扫一眼就知道是哪个号收到的）
HEADER_COLORS = ('blue', 'wathet', 'turquoise', 'green', 'yellow', 'orange', 'red', 'carmine', 'violet', 'indigo')
DEFAULT_HEADER_COLOR = 'blue'


def pick_header_color(seed: str) -> str:
    """按稳定哈希挑一个固定的标题配色（同一个账号每次都是同一个颜色）。"""
    if not seed:
        return DEFAULT_HEADER_COLOR
    return HEADER_COLORS[crc32(seed.encode()) % len(HEADER_COLORS)]


def format_details(details: dict[str, str]) -> str:
    """把明细拼成多行小字，一个属性一行（取不到的字段不占行）。"""
    return '\n'.join(f'**{key}** {value}' for key, value in details.items() if value)


# 正文里要变成可点链接的地址（http/https）。`fleamarket://` 这类 App 深链不转：
# 飞书不认这个协议，转成 markdown 链接会变成死链，原样留文本还能复制到手机上打开。
_URL_PATTERN = compile_pattern(r'https?://[^\s<>()\[\]"\']+')
# 已经是 markdown 链接/图片的部分：![文字](链接)、[文字](链接)。
# 必须整段跳过，否则链接化会把 `![图](url)` 拆成 `![图]([url](url))`。
_MARKDOWN_LINK_PATTERN = compile_pattern(r'!?\[[^\]]*\]\([^)]*\)')
_TOKEN_PATTERN = compile_pattern(rf'{_MARKDOWN_LINK_PATTERN.pattern}|{_URL_PATTERN.pattern}')


def linkify_urls(text: str) -> str:
    """把正文里的 http(s) 地址变成 markdown 链接（飞书里可点开看大图/视频）。

    链接文字就用地址本身，所以渲染出来的样子和纯文本一致，只是多了一层可点性 ——
    网页消息流里同一份文案仍是纯文本，两边不会出现两种说法。
    已经是 markdown 链接/图片语法的部分原样保留，不重复包裹。
    """
    return _TOKEN_PATTERN.sub(repl=_to_markdown_link, string=text)


def _to_markdown_link(match) -> str:
    token = match.group()
    if token.startswith(('[', '![')):  # 已经是链接/图片语法
        return token
    return f'[{token}]({token})'


def build_card(
    title: str,
    content: str,
    details: dict[str, str] | None = None,
    color: str = DEFAULT_HEADER_COLOR,
    video: dict[str, str] | None = None,
) -> dict:
    """富文本卡片：标题标明消息流向，明细是多行小号灰字，正文用普通文本，可内嵌视频。

    - 明细用 markdown 的 notation 字号（小号灰字）+ 分割线跟正文分开，比代码框干净：
      飞书会在代码框上多加一行「N 行代码」，那行没有意义；
    - `video` 传 {'file_key': .., 'img_key': ..} 时内嵌视频组件。视频组件要求卡片
      关掉转发（`enable_forward: false`），否则整张卡片发不出去（官方文档明确要求）。

    用卡片 2.0 的 markdown / video 组件（1.0 的 lark_md 连字号、分割线都控制不了）。
    """
    elements: list[dict] = []
    line = format_details(details or {})
    if line:
        elements.append({'tag': 'markdown', 'content': line, 'text_size': 'notation'})
        if content:
            elements.append({'tag': 'hr'})
    if content:
        elements.append({'tag': 'markdown', 'content': linkify_urls(content)})
    if video:
        element: dict = {'tag': 'video', 'file_key': video.get('file_key', ''), 'show_time': True}
        if video.get('img_key'):
            element['cover'] = {'img_key': video['img_key']}
        elements.append(element)
    config: dict = {'update_multi': True}
    if video:
        config['enable_forward'] = False
    return {
        'schema': '2.0',
        'config': config,
        'header': {'title': {'tag': 'plain_text', 'content': title}, 'template': color},
        'body': {'direction': 'vertical', 'elements': elements},
    }


def build_text_message(message: str) -> dict:
    """纯文本消息体（告警等不需要卡片格式的场景）。"""
    return {'text': message}

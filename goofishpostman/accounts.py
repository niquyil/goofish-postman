"""把各账号收到的私信汇总推送到同一个飞书机器人。"""

from __future__ import annotations

from time import time
from typing import TYPE_CHECKING

from httpx import AsyncClient, HTTPError, TimeoutException
from loguru import logger

from .goofish_utils import CONTENT_TYPE_LABELS
from .sender import DEFAULT_HEADER_COLOR, FEISHU_HEADERS, build_card_payload, build_text_payload, build_webhook_url

if TYPE_CHECKING:
    from collections.abc import Sequence

    from .types import MessageInfo

# 飞书开放平台（自建应用）接口：换 token 与上传图片
TENANT_TOKEN_URL = 'https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal'
UPLOAD_IMAGE_URL = 'https://open.feishu.cn/open-apis/im/v1/images'
# token 有效期 2 小时，这里提前 5 分钟续期
_TOKEN_SAFETY_MARGIN = 300
# 同一张图只上传一次；保留条数上限，避免长期运行无限增长
_MAX_IMAGE_CACHE = 200
# 单张图片下载上限（闲鱼原图一般几百 KB，给足余量）
_MAX_IMAGE_BYTES = 10 * 1024 * 1024
# 图片内嵌成功后要一并去掉的标注行：正文里那句 `[图片]` 的作用是告诉网页"这是图片"，
# 图片本身都显示出来了就不必再留一句标注
_IMAGE_LABEL_LINES = frozenset({f'[{CONTENT_TYPE_LABELS[2]}]'})
# 下载闲鱼图片要带 Referer/UA：不带的话部分地址直接返回 420（实测换头后 200）
IMAGE_DOWNLOAD_HEADERS = {
    'Referer': 'https://www.goofish.com/',
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36'
    ),
}


class NotifyError(RuntimeError):
    """推送失败（网络问题或飞书返回非 0 错误码）。"""


def format_direction(account_label: str, info: MessageInfo, sender: str = '') -> str:
    """消息流向：「发送方 → 接收方」，接收方写成「昵称(账号)」。"""
    who = sender or info.get('send_user_name') or info.get('send_user_id') or '未知用户'
    return f'{who} → {account_label}'


def format_message(account_label: str, info: MessageInfo, text: str, sender: str = '') -> str:
    """纯文本形态的飞书消息（卡片发不出去时的兜底、以及测试用）。

    形如：
        网课学习私人助理 → xy773249480508(2221114099805)
        在吗
    """
    return f'{format_direction(account_label, info, sender)}\n{text}'


def is_response_ok(body: dict) -> bool:
    """飞书 webhook 成功时 code=0；部分网关返回 StatusCode。两者都没有才算失败。"""
    if body.get('code') is not None:
        return body['code'] == 0
    if body.get('StatusCode') is not None:
        return body['StatusCode'] == 0
    return False


def build_image_markdown(image_key: str, alt: str = '图片') -> str:
    """卡片 markdown 里的内嵌图片。

    飞书只认自建应用上传得到的 image_key，外部 http 地址会被拒收整张卡片
    （实测 ErrCode 200570 invalid image keys），所以这里必须传 key。
    """
    return f'![{alt}]({image_key})'


def drop_lines(text: str, values: set[str]) -> str:
    """去掉正文里已经被内嵌图片取代的那些地址行。"""
    return '\n'.join(line for line in text.splitlines() if line.strip() not in values)


class FeishuNotifier:
    """多账号共用一个机器人；未配置 uuid 时所有发送都是空操作。

    另外可选配「企业自建应用」的 app_id / app_secret：飞书卡片不接受外部图片地址，
    图片要先经自建应用上传换成 image_key 才能在卡片里直接显示。
    """

    def __init__(
        self, uuid: str = '', secret: str = '', app_id: str = '', app_secret: str = '', timeout: float = 10.0
    ) -> None:
        self.uuid = uuid.strip()
        self.secret = secret.strip()
        self.app_id = app_id.strip()
        self.app_secret = app_secret.strip()
        self.timeout = timeout
        self._client: AsyncClient | None = None
        self._media_client: AsyncClient | None = None
        self._token: str = ''
        self._token_expires_at: float = 0.0
        self._image_keys: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return bool(self.uuid)

    @property
    def webhook(self) -> str:
        return build_webhook_url(self.uuid)

    @property
    def can_upload_images(self) -> bool:
        return bool(self.app_id and self.app_secret)

    def _get_client(self) -> AsyncClient:
        # httpx 的 AsyncClient 可以跨事件循环复用，这里按需创建并缓存
        if self._client is None or self._client.is_closed:
            self._client = AsyncClient(headers=FEISHU_HEADERS, timeout=self.timeout)
        return self._client

    def _get_media_client(self) -> AsyncClient:
        """下载图片 / 上传图片专用的客户端。

        不能复用带 `Content-Type: application/json` 默认头的那一个：httpx 会把这个头
        一起发出去，飞书上传接口据此当成 JSON 解析，直接报 234001 Invalid request param
        （实测：带 JSON 默认头 234001，换成干净客户端就能过参数校验）。
        """
        if self._media_client is None or self._media_client.is_closed:
            self._media_client = AsyncClient(headers=IMAGE_DOWNLOAD_HEADERS, timeout=self.timeout)
        return self._media_client

    async def close(self) -> None:
        for client in (self._client, self._media_client):
            if client is not None and not client.is_closed:
                await client.aclose()
        self._client = None
        self._media_client = None
        self._token = ''
        self._token_expires_at = 0.0

    async def send(self, message: str) -> None:
        """纯文本推送（告警等不需要代码框的场景）。"""
        await self._post(build_text_payload(message, self.secret))

    async def send_card(
        self,
        title: str,
        content: str,
        details: dict[str, str] | None = None,
        color: str = DEFAULT_HEADER_COLOR,
        images: Sequence[str] = (),
    ) -> None:
        """富文本卡片：标题写流向，正文是普通文本，details 放前面的代码框里（时间/商品名）。

        images 里是图片地址：能上传成功的会内嵌显示（正文里对应的地址行被替换掉），
        传不上去的原样保留成可点链接 —— 没配自建应用时就是这个行为。
        """
        body = await self._inline_images(content, images)
        await self._post(build_card_payload(title, body, details, self.secret, color))

    async def _inline_images(self, content: str, images: Sequence[str]) -> str:
        """把图片地址换成内嵌图片，返回新的正文。"""
        if not images or not self.can_upload_images:
            return content
        markdown: list[str] = []
        replaced: set[str] = set()
        for url in images:
            image_key = await self.resolve_image_key(url)
            if image_key:
                markdown.append(build_image_markdown(image_key))
                replaced.add(url)
        if not markdown:
            return content
        # 内嵌成功的图片，连同正文里那句 `[图片]` 标注一起去掉：图片就在眼前，不用再标一遍
        head = drop_lines(content, replaced | _IMAGE_LABEL_LINES)
        return '\n'.join(part for part in [head, *markdown] if part)

    async def resolve_image_key(self, url: str) -> str | None:
        """把图片地址换成飞书的 image_key（同一张图只上传一次）；失败返回 None。"""
        if not self.can_upload_images:
            return None
        cached = self._image_keys.get(url)
        if cached:
            return cached
        try:
            response = await self._get_media_client().get(url)
            response.raise_for_status()
            data = response.content
        except (HTTPError, TimeoutException, ValueError) as e:
            logger.warning(f'下载图片失败，改为只发链接: {url} ({type(e).__name__}: {e})')
            return None
        if not data or len(data) > _MAX_IMAGE_BYTES:
            logger.warning(f'图片大小异常（{len(data)} 字节），改为只发链接: {url}')
            return None
        try:
            image_key = await self.upload_image(data)
        except NotifyError as e:
            logger.warning(f'上传图片到飞书失败，改为只发链接: {e}')
            return None
        if len(self._image_keys) >= _MAX_IMAGE_CACHE:
            self._image_keys.pop(next(iter(self._image_keys)))
        self._image_keys[url] = image_key
        return image_key

    async def get_tenant_token(self) -> str:
        """自建应用 token（上传图片用），带缓存与提前续期。"""
        if self._token and time() < self._token_expires_at:
            return self._token
        try:
            response = await self._get_client().post(
                TENANT_TOKEN_URL, json={'app_id': self.app_id, 'app_secret': self.app_secret}
            )
            body = response.json()
        except (HTTPError, TimeoutException, ValueError) as e:
            raise NotifyError(f'获取飞书应用 token 失败: {e}') from e
        if body.get('code') != 0 or not body.get('tenant_access_token'):
            raise NotifyError(f'获取飞书应用 token 失败: {body}')
        self._token = body['tenant_access_token']
        self._token_expires_at = time() + max(0, int(body.get('expire', 7200)) - _TOKEN_SAFETY_MARGIN)
        return self._token

    async def upload_image(self, data: bytes) -> str:
        """上传图片，返回 image_key（卡片内嵌图片必须用它）。

        必须用不带 JSON 默认头的 media 客户端：否则 httpx 会把
        `Content-Type: application/json` 一起发出去，飞书按 JSON 解析 multipart 请求体，
        直接报 234001 Invalid request param（实测）。
        """
        token = await self.get_tenant_token()
        try:
            response = await self._get_media_client().post(
                UPLOAD_IMAGE_URL,
                headers={'Authorization': f'Bearer {token}'},
                data={'image_type': 'message'},
                files={'image': ('message.jpg', data, 'image/jpeg')},
            )
            body = response.json()
        except (HTTPError, TimeoutException, ValueError) as e:
            raise NotifyError(f'上传图片失败: {e}') from e
        image_key = (body.get('data') or {}).get('image_key')
        if body.get('code') != 0 or not image_key:
            raise NotifyError(f'上传图片失败: {body}')
        return image_key

    async def _post(self, payload: dict) -> None:
        if not self.configured:
            logger.debug('未配置飞书机器人 uuid，跳过推送')
            return
        try:
            response = await self._get_client().post(self.webhook, json=payload)
            body = response.json()
        except (HTTPError, TimeoutException, ValueError) as e:
            raise NotifyError(f'飞书推送失败: {e}') from e
        if not is_response_ok(body):
            raise NotifyError(f'飞书返回错误: {body}')
        logger.debug(f'飞书推送成功: {body.get("msg")}')

    async def send_account_message(
        self,
        account_label: str,
        info: MessageInfo,
        text: str,
        sender: str = '',
        details: dict[str, str] | None = None,
        color: str = DEFAULT_HEADER_COLOR,
        images: Sequence[str] = (),
    ) -> None:
        """推送一条账号收到的私信：标题是「发送方 → 接收方」，正文是消息内容，
        details（时间 / 商品名）放进正文前面的代码框，images 是能内嵌的图片地址。

        account_label 为接收账号的「昵称(账号)」，sender 为发送方昵称。
        """
        await self.send_card(format_direction(account_label, info, sender), text, details, color, images)

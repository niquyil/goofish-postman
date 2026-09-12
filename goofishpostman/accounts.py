"""把各账号收到的私信汇总推送到同一个飞书机器人。"""

from __future__ import annotations

from typing import TYPE_CHECKING

from httpx import AsyncClient, HTTPError, TimeoutException
from loguru import logger

from .sender import DEFAULT_HEADER_COLOR, FEISHU_HEADERS, build_card_payload, build_text_payload, build_webhook_url

if TYPE_CHECKING:
    from .types import MessageInfo


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


class FeishuNotifier:
    """多账号共用一个机器人；未配置 uuid 时所有发送都是空操作。"""

    def __init__(self, uuid: str = '', secret: str = '', timeout: float = 10.0) -> None:
        self.uuid = uuid.strip()
        self.secret = secret.strip()
        self.timeout = timeout
        self._client: AsyncClient | None = None

    @property
    def configured(self) -> bool:
        return bool(self.uuid)

    @property
    def webhook(self) -> str:
        return build_webhook_url(self.uuid)

    def _get_client(self) -> AsyncClient:
        # httpx 的 AsyncClient 可以跨事件循环复用，这里按需创建并缓存
        if self._client is None or self._client.is_closed:
            self._client = AsyncClient(headers=FEISHU_HEADERS, timeout=self.timeout)
        return self._client

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def send(self, message: str) -> None:
        """纯文本推送（告警等不需要代码框的场景）。"""
        await self._post(build_text_payload(message, self.secret))

    async def send_card(
        self, title: str, content: str, details: dict[str, str] | None = None, color: str = DEFAULT_HEADER_COLOR
    ) -> None:
        """富文本卡片：标题写流向，正文是普通文本，details 放前面的代码框里（时间/商品名）。"""
        await self._post(build_card_payload(title, content, details, self.secret, color))

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
    ) -> None:
        """推送一条账号收到的私信：标题是「发送方 → 接收方」，正文是消息内容，
        details（时间 / 商品名）放进正文前面的代码框。

        account_label 为接收账号的「昵称(账号)」，sender 为发送方昵称。
        """
        await self.send_card(format_direction(account_label, info, sender), text, details, color)

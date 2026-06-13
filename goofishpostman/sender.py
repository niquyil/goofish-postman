from base64 import b64encode
from functools import cached_property
from hashlib import sha256
from hmac import new
from time import time

from aiohttp import ClientSession
from loguru import logger


class Sender:
    headers = {'Content-Type': 'application/json'}

    def __init__(self, uuid: str, secret: str | None = None) -> None:
        self.uuid = uuid
        self.secret = secret

    @cached_property
    def webhook(self) -> str:
        return f'https://open.feishu.cn/open-apis/bot/v2/hook/{self.uuid}'

    def generate_sign(self, timestamp: int) -> str:
        hmac_code = new(key=f'{timestamp}\n{self.secret}'.encode('utf-8'), digestmod=sha256).digest()
        return b64encode(hmac_code).decode('utf-8')

    async def send_message(self, message: str) -> None:
        payload = {'msg_type': 'text', 'content': {'text': message}}
        if self.secret is not None:
            timestamp = int(time())
            payload.update({'timestamp': str(timestamp), 'sign': self.generate_sign(timestamp)})
        async with ClientSession() as session:
            async with session.post(url=self.webhook, json=payload, headers=self.headers) as response:
                logger.debug((await response.json())['msg'])

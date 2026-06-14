from __future__ import annotations

from asyncio import run
from json import loads
from os import getenv
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from execjs import ProgramError
from loguru import logger

from .goofish_live import GoofishLive
from .goofish_utils import decrypt
from .path import ENV_FILE
from .sender import Sender

if TYPE_CHECKING:
    from websockets import ClientConnection

class GoofishPostman(GoofishLive):
    def __init__(self, cookies_str: str, sender: Sender) -> None:
        super().__init__(cookies_str)
        self.username = self.cookies['tracknick']
        self.sender = sender

    async def handle_message(self, message, websocket: ClientConnection) -> None:
        try:
            data = message['body']['syncPushPackage']['data'][0]['data']
        except KeyError:
            pass
        else:
            try:
                info = loads(decrypt(data))['1']
                if isinstance(info, dict):
                    send_user_name = info['10'].get('reminderTitle')
                    send_message = info['10'].get('reminderContent')
                    if send_user_name and send_message:
                        logger.info(f'{self.username}收到来自{send_user_name}的信息: {send_message}')
                        await self.sender.send_message(
                            message=f'{self.username}收到来自{send_user_name}的信息: {send_message}'
                        )
            except ProgramError:
                pass


if __name__ == '__main__':
    load_dotenv(ENV_FILE)
    messenger = GoofishPostman(
        cookies_str=getenv('COOKIE_STR'), sender=Sender(uuid=getenv('UUID'), secret=getenv('SECRET'))
    )
    run(messenger.main())

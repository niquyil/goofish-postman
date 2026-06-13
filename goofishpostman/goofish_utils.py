from __future__ import annotations

from pathlib import Path
from pkgutil import get_data
from typing import TYPE_CHECKING

import execjs
from loguru import logger

from . import __package__ as package

if TYPE_CHECKING:
    from requests import Session

goofish_js = execjs.compile(
    get_data(package=package, resource=str(Path('script') / 'goofish_js.js')).decode()
)


def trans_cookies(cookies_str: str):
    cookies = dict()
    for cookie in cookies_str.split('; '):
        try:
            key, *value = cookie.split('=')
            cookies[key] = '='.join(value)
        except Exception as e:
            logger.error(e)
    return cookies


def trans_cookies_str(cookies_dict: dict) -> str:
    return '; '.join(f'{key}={value}' for key, value in cookies_dict.items())


def get_session_cookies(session: Session) -> dict[str, str | None]:
    return session.cookies.get_dict()


def get_session_cookies_str(session: Session) -> str:
    return '; '.join(f'{key}={value}' for key, value in session.cookies.get_dict().items())


def generate_mid() -> str:
    return goofish_js.call('generate_mid')


def generate_uuid() -> str:
    return goofish_js.call('generate_uuid')


def generate_device_id(user_id: str) -> str:
    return goofish_js.call('generate_device_id', user_id)


def generate_sign(timestamp: str, token: str, data: str) -> str:
    return goofish_js.call('generate_sign', timestamp, token, data)


def decrypt(string: str) -> str:
    return goofish_js.call('decrypt', string)

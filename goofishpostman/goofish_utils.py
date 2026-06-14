from pathlib import Path
from pkgutil import get_data
from uuid import uuid4

import execjs

from . import __package__ as package

goofish_js = execjs.compile(get_data(package=package, resource=str(Path('script') / 'goofish.js')).decode())


def generate_mid() -> str:
    # return f'{randint(a=0, b=999)}{int(time()*1000)} 0'
    return goofish_js.call('generate_mid')


def generate_uuid() -> str:
    return goofish_js.call('generate_uuid')


def generate_device_id(user_id: str | None) -> str:
    return f'{str(uuid4()).upper()}-{user_id}'


def decrypt(string: str) -> str:
    return goofish_js.call('decrypt', string)

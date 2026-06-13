from decimal import Decimal
from typing import Literal

from pydantic import BaseModel
from typing_extensions import TypedDict


class Message(BaseModel):
    type: Literal['audio', 'image', 'text']


class AudioMessage(Message):
    type: Literal['audio'] = 'audio'
    url: str
    duration: int


class ImageMessage(Message):
    type: Literal['image'] = 'image'
    url: str
    width: int
    height: int


class TextMessage(Message):
    type: Literal['text'] = 'text'
    text: str


def make_text(text: str) -> TextMessage:
    return TextMessage(text=text)


def make_image(url: str, width: int = 0, height: int = 0) -> ImageMessage:
    return ImageMessage(url=url, width=width, height=height)


def make_audio(url: str, duration: int = 0) -> AudioMessage:
    return AudioMessage(url=url, duration=duration)


class Price(BaseModel):
    current: Decimal = Decimal()
    original: Decimal = Decimal()


class Delivery(BaseModel):
    # 包邮\按距离计费\一口价\无需邮寄 四选一
    method: Literal['包邮', '按距离计费', '一口价', '无需邮寄']
    price: Decimal = Decimal()
    self_pickup_accepted: bool = False

from __future__ import annotations

from base64 import b64encode
from decimal import Decimal
from enum import StrEnum
from json import dumps
from typing import Any, Literal, TypedDict

from pydantic import BaseModel

# mtop 接口的 appKey（签名与 query 参数都用它）
MTOP_APP_KEY = '34839810'
# 长连接 /reg 的 app-key
APP_KEY = '444e9908a51d1cb236a27862abc769c9'


class MessageInfo(TypedDict):
    """解密后的私信推送里的关键字段。"""

    cid: str
    send_user_id: str
    send_user_name: str
    send_message: str
    raw: dict[str, Any]


class MessageContent(TypedDict):
    """私信正文的展示形态（网页消息流与飞书卡片共用同一份结构）。

    - label：类型标注，如 `[图片]`；文本消息为空串
    - lines：附加说明（卡片标题、提示语等，HTML 已去掉、内嵌链接已转成「文字（url）」）
    - links：(显示名, 链接)，显示名为空表示直接用链接本身当文案

    字段名用名词，取用它的函数（format_content_text 等）负责拼装。
    """

    label: str
    lines: list[str]
    links: list[tuple[str, str]]


class DeliveryMethod(StrEnum):
    """配送方式，取值与闲鱼发布接口一致。"""

    free_shipping = '包邮'
    by_distance = '按距离计费'
    fixed_price = '一口价'
    no_shipping = '无需邮寄'


def encode_b64_json(data: Any) -> str:
    """闲鱼协议的 custom.data：UTF-8 JSON 的 base64。"""
    return b64encode(dumps(data, separators=(',', ':')).encode('utf-8')).decode('utf-8')


class Message(BaseModel):
    """待发送的消息，`to_custom()` 直接产出协议里的 custom 字段。"""

    type: Literal['audio', 'image', 'text']
    content_type: int = 0

    def to_payload(self) -> dict[str, Any]:
        raise NotImplementedError

    def to_custom(self) -> tuple[int, str]:
        return self.content_type, encode_b64_json(self.to_payload())


class AudioMessage(Message):
    type: Literal['audio'] = 'audio'
    content_type: int = 3
    url: str
    duration: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {'contentType': self.content_type, 'audio': {'url': self.url, 'duration': self.duration}}


class ImageMessage(Message):
    type: Literal['image'] = 'image'
    content_type: int = 2
    url: str
    width: int = 0
    height: int = 0

    def to_payload(self) -> dict[str, Any]:
        return {
            'contentType': self.content_type,
            'image': {'pics': [{'type': 0, 'url': self.url, 'width': self.width, 'height': self.height}]},
        }


class TextMessage(Message):
    type: Literal['text'] = 'text'
    content_type: int = 1
    text: str

    def to_payload(self) -> dict[str, Any]:
        return {'contentType': self.content_type, 'text': {'text': self.text}}


def make_text(text: str) -> TextMessage:
    return TextMessage(text=text)


def make_image(url: str, width: int = 0, height: int = 0) -> ImageMessage:
    return ImageMessage(url=url, width=width, height=height)


def make_audio(url: str, duration: int = 0) -> AudioMessage:
    return AudioMessage(url=url, duration=duration)


class Price(BaseModel):
    current: Decimal = Decimal(0)
    original: Decimal = Decimal(0)

    def to_cents(self) -> dict[str, str]:
        """转成发布接口需要的分单位字段。"""
        fields: dict[str, str] = {}
        if self.current > 0:
            fields['priceInCent'] = str(int(self.current * 100))
        if self.original > 0:
            fields['origPriceInCent'] = str(int(self.original * 100))
        return fields


class Delivery(BaseModel):
    # 包邮\按距离计费\一口价\无需邮寄 四选一
    method: DeliveryMethod
    price: Decimal = Decimal(0)
    self_pickup_accepted: bool = False

    def to_post_fee(self) -> dict[str, Any]:
        """转成发布接口需要的 itemPostFeeDTO 字段。

        注意：`self_pickup_accepted` 不在这里输出 —— 原实现把 onlyTakeSelf 写在了
        body 根部（见 Goofish.publish），为保持线上行为一致，这里不加。
        """
        fields: dict[str, Any] = {'canFreeShipping': False, 'supportFreight': False, 'onlyTakeSelf': False}
        match self.method:
            case DeliveryMethod.free_shipping:
                fields['canFreeShipping'] = True
                fields['supportFreight'] = True
            case DeliveryMethod.by_distance:
                fields['supportFreight'] = True
                fields['templateId'] = '-100'
            case DeliveryMethod.fixed_price:
                fields['supportFreight'] = True
                fields['postPriceInCent'] = str(int(self.price * 100))
                fields['templateId'] = '0'
            case DeliveryMethod.no_shipping:
                fields['templateId'] = '0'
        return fields


class ImageInfo(BaseModel):
    """已上传到闲鱼的图片，用于发布/推荐接口。"""

    url: str
    width: int
    height: int

    def to_image_info_do(self) -> dict[str, Any]:
        return {
            'extraInfo': {'isH': 'false', 'isT': 'false', 'raw': 'false'},
            'isQrCode': False,
            'url': self.url,
            'heightSize': self.height,
            'widthSize': self.width,
            'major': True,
            'type': 0,
            'status': 'done',
        }

from __future__ import annotations

from base64 import b64decode
from json import dumps, loads

from pytest import MonkeyPatch, mark
from requests.cookies import RequestsCookieJar

from goofishpostman.goofish_apis import API, Goofish
from goofishpostman.goofish_live import build_ack
from goofishpostman.types import (
    APP_KEY,
    MTOP_APP_KEY,
    Delivery,
    DeliveryMethod,
    ImageInfo,
    ImageMessage,
    Price,
    TextMessage,
)


def custom_payload(message: TextMessage | ImageMessage) -> dict:
    content_type, data = message.to_custom()
    return {'type': content_type, 'payload': loads(b64decode(data))}


# ── 发送消息 ──────────────────────────────────────────────────────────────────


def test_text_message_payload() -> None:
    assert custom_payload(TextMessage(text='你好')) == {
        'type': 1,
        'payload': {'contentType': 1, 'text': {'text': '你好'}},
    }


def test_image_message_payload() -> None:
    assert custom_payload(ImageMessage(url='http://x/1.png', width=3, height=4)) == {
        'type': 2,
        'payload': {
            'contentType': 2,
            'image': {'pics': [{'type': 0, 'url': 'http://x/1.png', 'width': 3, 'height': 4}]},
        },
    }


# ── mtop 请求 ─────────────────────────────────────────────────────────────────


def test_sign_format() -> None:
    """sign = md5(token&t&appKey&data)，其中 appKey 是 mtop 的 34839810。"""
    from hashlib import md5

    data = '{"itemId":"1"}'
    expected = md5(f'TOKEN&1700000000000&{MTOP_APP_KEY}&{data}'.encode()).hexdigest()
    assert Goofish.sign('1700000000000', 'TOKEN', data) == expected


def test_get_token_body_uses_channel_app_key() -> None:
    """换取长连接 token 的 body.appKey 是钉钉的 app-key，且 sign 里的仍是 mtop appKey。"""
    captured = {}

    class Session:
        cookies = RequestsCookieJar()

        def post(self, **kwargs):
            captured.update(kwargs)
            return make_response()

    api = Goofish.__new__(Goofish)
    api.session = Session()
    api.device_id = 'DEV'
    api.get_token()
    assert captured['data']['data'] == dumps({'appKey': APP_KEY, 'deviceId': 'DEV'}, separators=(',', ':'))
    assert captured['params']['appKey'] == MTOP_APP_KEY


def test_mtop_params_defaults() -> None:
    params = API['item_detail'].build_params(data='{}', timestamp='T', sign='S')
    assert params['appKey'] == MTOP_APP_KEY
    assert params['api'] == 'mtop.taobao.idle.pc.detail'
    assert params['sign'] == 'S'
    assert params['jsv'] == '2.7.2'


@mark.parametrize('name', sorted(API))
def test_api_spec_self_consistent(name: str) -> None:
    spec = API[name]
    assert spec.url.endswith(f'/{spec.version}/')
    assert spec.api in spec.url
    assert spec.build_headers()['User-Agent']  # headers() 不应抛错


def test_timestamp_is_milliseconds(monkeypatch: MonkeyPatch) -> None:
    """原 get_default_location 漏了 *1000，重构后统一走 _call_mtop，必须是毫秒。"""
    captured = {}

    class Session:
        cookies = RequestsCookieJar()

        def post(self, **kwargs):
            captured.update(kwargs)
            return make_response()

    api = Goofish.__new__(Goofish)
    api.session = Session()
    monkeypatch.setattr('goofishpostman.goofish_apis.time', lambda: 1700000000.0)
    api.get_default_location()
    assert captured['params']['t'] == '1700000000000'


# ── 发布参数 ──────────────────────────────────────────────────────────────────


def make_response(payload: dict | None = None):
    class Response:
        cookies = RequestsCookieJar()

        @staticmethod
        def json():
            return payload if payload is not None else {'ret': ['SUCCESS::'], 'data': {}}

    return Response()


def test_price_to_cents() -> None:
    assert Price(current=12.34, original=99).to_cents() == {'priceInCent': '1234', 'origPriceInCent': '9900'}
    assert Price().to_cents() == {}


@mark.parametrize(
    ('method', 'expected'),
    [
        (DeliveryMethod.free_shipping, {'canFreeShipping': True, 'supportFreight': True, 'templateId': None}),
        (DeliveryMethod.by_distance, {'supportFreight': True, 'templateId': '-100'}),
        (DeliveryMethod.fixed_price, {'supportFreight': True, 'templateId': '0', 'postPriceInCent': '550'}),
        (DeliveryMethod.no_shipping, {'templateId': '0'}),
        ('包邮', {'canFreeShipping': True, 'supportFreight': True}),  # 字符串字面量同样可用
    ],
)
def test_delivery_to_post_fee(method: DeliveryMethod, expected: dict) -> None:
    fields = Delivery(method=method, price=5.5).to_post_fee()
    for key, value in expected.items():
        if value is None:
            assert key not in fields
        else:
            assert fields[key] == value
    # 保持原行为：self_pickup_accepted 由 publish 写到 body 根部，这里恒为 False
    assert fields['onlyTakeSelf'] is False


def test_image_info_do() -> None:
    assert ImageInfo(url='u', width=1, height=2).to_image_info_do() == {
        'extraInfo': {'isH': 'false', 'isT': 'false', 'raw': 'false'},
        'isQrCode': False,
        'url': 'u',
        'heightSize': 2,
        'widthSize': 1,
        'major': True,
        'type': 0,
        'status': 'done',
    }


# ── 长连接 ────────────────────────────────────────────────────────────────────


def test_build_ack_echoes_headers() -> None:
    ack = build_ack({'headers': {'mid': 'M', 'sid': 'S', 'app-key': 'K', 'ua': 'UA', 'dt': 'j', 'other': 'x'}})
    assert ack['code'] == 200
    assert ack['headers'] == {'mid': 'M', 'sid': 'S', 'app-key': 'K', 'ua': 'UA', 'dt': 'j'}


def test_build_ack_fills_missing_mid_and_sid() -> None:
    ack = build_ack({'headers': {}})
    assert ack['headers']['mid']
    assert ack['headers']['sid'] == ''


# 私信解析的测试见 tests/test_message_parsing.py（用真实抓包报文）

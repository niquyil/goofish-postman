from __future__ import annotations

from dataclasses import dataclass
from functools import partial
from json import dumps
from pathlib import Path
from subprocess import run
from time import sleep, time
from typing import Any

from loguru import logger
from requests import Session

from .cookies import Cookies
from .goofish_utils import generate_device_id, generate_sign
from .headers import SEC_CH_UA, USER_AGENT
from .qrlogin import (
    STATUS_CONFIRMED,
    STATUS_EXPIRED,
    STATUS_TEXT,
    QrSession,
    finish_qr_login,
    poll_qr_login,
    render_qrcode,
    start_qr_login,
)
from .types import APP_KEY, MTOP_APP_KEY, Delivery, ImageInfo, Price

_HERE = Path(__file__).resolve().parent
_SCRIPT_DIR = _HERE / 'script'
_MINI_LOGIN_URL = 'https://passport.goofish.com/mini_login.htm'

# ── mtop 接口公共部分 ─────────────────────────────────────────────────────────
_MTOP_BASE_HEADERS = {
    'Accept': 'application/json',
    'Accept-Language': 'en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Content-Type': 'application/x-www-form-urlencoded',
    'Origin': 'https://www.goofish.com',
    'Priority': 'u=1, i',
    'Referer': 'https://www.goofish.com/',
    'Sec-Ch-Ua': SEC_CH_UA,
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-site',
    'User-Agent': USER_AGENT,
}

# 各接口与 _MTOP_BASE_HEADERS 的差异（get_default_location / get_publish_channel / get_token / upload_media 没有 Cache-Control）
_MTOP_HEADER_OVERRIDES: dict[str, dict[str, str]] = {
    'mtop.taobao.idlemessage.pc.login.token': {'Cache-Control': 'no-cache', 'Host': 'h5api.m.goofish.com'}
}
_MTOP_CACHE_HEADER = {'Cache-Control': 'no-cache'}
# 这些接口的原始实现没带 Cache-Control / Pragma
_MTOP_NO_CACHE_HEADER_APIS = frozenset({'mtop.taobao.idle.local.poi.get', 'mtop.taobao.idle.kgraph.property.recommend'})

_PASSPORT_HEADERS = {
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'en,zh-CN;q=0.9,zh;q=0.8,zh-TW;q=0.7,ja;q=0.6',
    'Accept-Encoding': 'gzip, deflate, br, zstd',
    'Priority': 'u=1, i',
    'Sec-Ch-Ua': SEC_CH_UA,
    'Sec-Ch-Ua-Mobile': '?0',
    'Sec-Ch-Ua-Platform': '"Windows"',
    'Sec-Fetch-Dest': 'empty',
    'Sec-Fetch-Mode': 'cors',
    'Sec-Fetch-Site': 'same-origin',
    'User-Agent': USER_AGENT,
}


@dataclass(frozen=True, slots=True)
class MtopApi:
    """一个 mtop 接口的固定参数，签名所需的 data 由调用方动态生成。"""

    url: str
    version: str
    spm_cnt: str
    spm_pre: str
    log_id: str

    @property
    def api(self) -> str:
        return self.url.split('/h5/')[1].split('/')[0]

    def build_params(self, data: str, timestamp: str, sign: str) -> dict[str, str]:
        return {
            'jsv': '2.7.2',
            'appKey': MTOP_APP_KEY,
            't': timestamp,
            'sign': sign,
            'v': self.version,
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': self.api,
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': self.spm_cnt,
            'spm_pre': self.spm_pre,
            'log_id': self.log_id,
        }

    def build_headers(self) -> dict[str, str]:
        headers = _MTOP_BASE_HEADERS | _MTOP_HEADER_OVERRIDES.get(self.api, {})
        if self.api not in _MTOP_NO_CACHE_HEADER_APIS:
            headers = headers | _MTOP_CACHE_HEADER
        return headers


API = {
    'get_token': MtopApi(
        url='https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/',
        version='1.0',
        spm_cnt='a21ybx.im.0.0',
        spm_pre='a21ybx.item.want.1.14ad3da6ALVq3n',
        log_id='14ad3da6ALVq3n',
    ),
    'refresh_token': MtopApi(
        url='https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.loginuser.get/1.0/',
        version='1.0',
        spm_cnt='a21ybx.im.0.0',
        spm_pre='a21ybx.item.want.1.12523da6waCtUp',
        log_id='12523da6waCtUp',
    ),
    'item_detail': MtopApi(
        url='https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/',
        version='1.0',
        spm_cnt='a21ybx.im.0.0',
        spm_pre='a21ybx.item.want.1.12523da6waCtUp',
        log_id='12523da6waCtUp',
    ),
    'publish_channel': MtopApi(
        url='https://h5api.m.goofish.com/h5/mtop.taobao.idle.kgraph.property.recommend/2.0/',
        version='2.0',
        spm_cnt='a21ybx.publish.0.0',
        spm_pre='a21ybx.item.sidebar.1.67321598K9Vgx8',
        log_id='67321598K9Vgx8',
    ),
    'publish': MtopApi(
        url='https://h5api.m.goofish.com/h5/mtop.idle.pc.idleitem.publish/1.0/',
        version='1.0',
        spm_cnt='a21ybx.publish.0.0',
        spm_pre='a21ybx.home.sidebar.1.46413da6EPl7v5',
        log_id='46413da6EPl7v5',
    ),
    'default_location': MtopApi(
        url='https://h5api.m.goofish.com/h5/mtop.taobao.idle.local.poi.get/1.0/',
        version='1.0',
        spm_cnt='a21ybx.publish.0.0',
        spm_pre='a21ybx.item.sidebar.1.38262218ame5nr',
        log_id='38262218ame5nr',
    ),
}

# 鉴权类接口返回的 Set-Cookie 需要先清掉本地同名 cookie（domain='' / path='/'）才会生效
_TOKEN_COOKIE_APIS = frozenset({API['get_token'].api, API['refresh_token'].api})

api = partial(API.__getitem__)


def generate_tfstk(timeout: int = 15) -> str:
    """执行逆向出的 tfstk.js 取回 tfstk cookie 值（需要本机 node）。"""
    script = _SCRIPT_DIR / 'tfstk.js'
    if not script.exists():
        return ''
    try:
        result = run(['node', str(script)], capture_output=True, timeout=timeout, check=False)
        return result.stdout.decode().strip()
    except Exception as e:  # noqa: BLE001 - 拿不到 tfstk 不阻塞登录流程
        logger.error(e)
        return ''


def build_initial_cookies() -> Session:
    """纯 HTTP 获取闲鱼初始 cookie（不含登录态）"""
    session = Session()
    session.headers.update({'User-Agent': USER_AGENT})

    session.get(url='https://log.mmstat.com/eg.js', timeout=10)
    cna = session.cookies.get(name='cna', domain='.mmstat.com')
    if cna:
        session.cookies.set(name='cna', value=cna, domain='.goofish.com', path='/')

    for api_name in ('mtop.taobao.idlehome.home.webpc.feed', 'mtop.gaia.nodejs.gaia.idle.data.gw.v2.index.get'):
        session.post(
            url=f'https://h5api.m.goofish.com/h5/{api_name}/1.0/',
            params={
                'jsv': '2.7.2',
                'appKey': MTOP_APP_KEY,
                't': str(int(time() * 1000)),
                'sign': '',
                'v': '1.0',
                'type': 'originaljson',
                'dataType': 'json',
                'timeout': '20000',
                'api': api_name,
                'sessionOption': 'AutoLoginOnly',
                'spm_cnt': 'a21ybx.home.0.0',
            },
            data={'data': '{}'},
            headers=_MTOP_BASE_HEADERS,
            timeout=10,
        )

    tfstk = generate_tfstk()
    if tfstk:
        session.cookies.set(name='tfstk', value=tfstk, domain='.goofish.com', path='/')

    return session


def create_login_session() -> QrSession:
    """扫码第一步：建会话并加载 mini_login 页面，拿到 XSRF-TOKEN 等。"""
    session = build_initial_cookies()
    cna = (
        session.cookies.get(name='cna', domain='.goofish.com')
        or session.cookies.get(name='cna', domain='.mmstat.com')
        or ''
    )

    session.get(
        url=_MINI_LOGIN_URL,
        params={
            'lang': 'zh_cn',
            'appName': 'xianyu',
            'appEntrance': 'web',
            'styleType': 'vertical',
            'bizParams': '',
            'notLoadSsoView': 'false',
            'notKeepLogin': 'false',
            'isMobile': 'false',
            'qrCodeFirst': 'false',
            'stie': '77',
            'rnd': '0.6842814084442211',
        },
        headers=_PASSPORT_HEADERS
        | {
            'Referer': 'https://www.goofish.com/',
            'Sec-Fetch-Site': 'same-site',
            'Sec-Fetch-Dest': 'iframe',
            'Sec-Fetch-Mode': 'navigate',
        },
        timeout=15,
    )

    return QrSession(
        session=session,
        device_id=generate_device_id(),
        cna=cna,
        csrf_token=session.cookies.get(name='XSRF-TOKEN', domain='passport.goofish.com') or '',
    )


def login_with_qrcode(poll_interval: float = 3.0, timeout: float = 120.0, show_qrcode: bool = True):
    """扫码登录闲鱼（命令行用），返回已登录的 Goofish 实例。

    实现已迁到 qrlogin 模块；Web 端请直接用那里的分步接口：
    start_qr_login() / poll_qr_login() / finish_qr_login()，配合本模块的 create_login_session()。
    """
    from .qrlogin import build_session_cookie_string

    state = start_qr_login(create_login_session())
    print(f'[login_with_qrcode] QR URL: {state.qr_url}')
    print('[login_with_qrcode] 用闲鱼 App 扫码（左上角 -> 扫一扫）')
    if show_qrcode:
        render_qrcode(state.qr_url)

    deadline = time() + timeout
    last_status = ''
    while time() < deadline:
        status = poll_qr_login(state)
        if status != last_status:
            print(f'[login_with_qrcode] [{status}] {STATUS_TEXT.get(status, status)} ({int(deadline - time())}s left)')
            last_status = status
        if status == STATUS_CONFIRMED:
            break
        if status == STATUS_EXPIRED:
            raise TimeoutError('二维码已过期，请重新调用 login_with_qrcode()')
        sleep(poll_interval)
    else:
        raise TimeoutError('扫码超时，未完成登录')

    info = finish_qr_login(state)
    print(f'[login_with_qrcode] 登录成功！用户: {info["tracknick"]} (unb={info["unb"]})')
    login = Goofish(cookies=Cookies.from_str(build_session_cookie_string(state.session)), device_id=state.device_id)
    login.session = state.session
    return login


class Goofish:
    upload_media_url = 'https://stream-upload.goofish.com/api/upload.api'
    reset_login_info_url = 'https://passport.goofish.com/newlogin/hasLogin.do'

    def __init__(self, cookies, device_id: str):
        self.session = Session()
        self.session.cookies.update(cookies)
        self.device_id = device_id

    @staticmethod
    def sign(timestamp: str, token: str, data: str) -> str:
        return generate_sign(timestamp=timestamp, token=token, data=data)

    @property
    def cookies(self) -> Cookies:
        return Cookies.from_session(self.session)

    @property
    def mtop_token(self) -> str:
        return self.session.cookies.get(name='_m_h5_tk', default='').split('_')[0]

    def _call_mtop(self, spec: MtopApi, data: dict[str, Any] | str, *, retrying: bool = False) -> dict[str, Any]:
        """mtop 接口统一出入参：自动带时间戳、mtop token、sign，并清理过期 cookie。"""
        if not isinstance(data, str):
            data = dumps(data, separators=(',', ':'))
        timestamp = str(int(time() * 1000))
        params = spec.build_params(
            data=data, timestamp=timestamp, sign=self.sign(timestamp=timestamp, token=self.mtop_token, data=data)
        )
        response = self.session.post(
            url=spec.url, data={'data': data}, headers=spec.build_headers(), params=params, timeout=20
        )

        if spec.api in _TOKEN_COOKIE_APIS:
            for key in list(self.session.cookies):
                if key.name in response.cookies and key.domain == '' and key.path == '/':
                    self.session.cookies.clear(domain=key.domain, path=key.path, name=key.name)

        try:
            result = response.json()
        except ValueError as e:
            # 风控页 / 网关错误页都会走到这里：给一句能判断方向的话，别让上层只看到 JSONDecodeError
            raise RuntimeError(
                f'闲鱼接口返回了非 JSON 内容（HTTP {response.status_code}，{spec.api}），可能是网络问题或触发了风控'
            ) from e

        if not retrying and result.get('ret') and '令牌过期' in result['ret'][0]:
            # 服务端说 token 过期时会顺手下发新的 _m_h5_tk，带新 token 重试一次即可。
            # 只重试一次：一直回"令牌过期"说明登录态本身已经废了，继续递归只会变成 RecursionError
            return self._call_mtop(spec, data, retrying=True)
        return result

    def get_default_location(self) -> dict[str, Any]:
        return self._call_mtop(
            api('default_location'), {'longitude': 118.78248347393424, 'latitude': 31.91629189813543}
        )

    def get_item_info(self, item_id: str) -> dict[str, Any]:
        return self._call_mtop(api('item_detail'), {'itemId': item_id})

    def get_publish_channel(self, title: str, images_info: list[ImageInfo]) -> dict[str, Any]:
        data = {
            'title': title,
            'lockCpv': False,
            'multiSKU': False,
            'publishScene': 'mainPublish',
            'scene': 'newPublishChoice',
            'description': title,
            'imageInfos': [image.to_image_info_do() for image in images_info],
            'uniqueCode': '1775905618164677',
        }
        return self._call_mtop(api('publish_channel'), data)

    def get_token(self) -> dict[str, Any]:
        # 注意：这里的 appKey 是长连接的 app-key，不是 mtop 的 appKey
        return self._call_mtop(api('get_token'), {'appKey': APP_KEY, 'deviceId': self.device_id})

    def refresh_token(self) -> dict[str, Any]:
        return self._call_mtop(api('refresh_token'), {})

    def publish(
        self, images_path: list[str], goods_desc: str, price: Price | None = None, delivery: Delivery | None = None
    ) -> dict[str, Any]:
        delivery = delivery or Delivery(method='无需邮寄')
        data: dict[str, Any] = {
            'freebies': False,
            'itemTypeStr': 'b',
            'quantity': '1',
            'simpleItem': 'true',
            'imageInfoDOList': [],
            'itemTextDTO': {'desc': goods_desc, 'title': goods_desc, 'titleDescSeparate': False},
            'itemLabelExtList': [],
            'itemPriceDTO': price.to_cents() if price else {},
            'userRightsProtocols': [{'enable': False, 'serviceCode': 'SKILL_PLAY_NO_MIND'}],
            'itemPostFeeDTO': delivery.to_post_fee(),
            'itemAddrDTO': {},
            'defaultPrice': price is None or not price.to_cents(),
            'itemCatDTO': {},
            'uniqueCode': '1775897582791680',
            'sourceId': 'pcMainPublish',
            'bizcode': 'pcMainPublish',
            'publishScene': 'pcMainPublish',
        }

        images_info = [self.upload_image(image_path) for image_path in images_path or []]
        data['imageInfoDOList'] = [image.to_image_info_do() for image in images_info]

        # 注意：接口读的是 itemPostFeeDTO.onlyTakeSelf，原实现写到了 body 根部的 onlyTakeSelf（无效字段），
        # 这里保持原样以免改变线上行为
        if delivery.self_pickup_accepted:
            data['onlyTakeSelf'] = True

        channel_res = self.get_publish_channel(goods_desc, images_info)
        for card in channel_res['data']['cardList']:
            card_data = card['cardData']
            for card_value in card_data.get('valuesList', []):
                if card_value.get('isClicked'):
                    data['itemLabelExtList'].append(
                        {
                            'channelCateName': card_value['catName'],
                            'valueId': None,
                            'channelCateId': card_value['channelCatId'],
                            'valueName': None,
                            'tbCatId': card_value['tbCatId'],
                            'subPropertyId': None,
                            'labelType': 'common',
                            'subValueId': None,
                            'labelId': None,
                            'propertyName': card_data['propertyName'],
                            'isUserClick': '1',
                            'isUserCancel': None,
                            'from': 'newPublishChoice',
                            'propertyId': card_data['propertyId'],
                            'labelFrom': 'newPublish',
                            'text': card_value['catName'],
                            'properties': f'{card_data["propertyId"]}##{card_data["propertyName"]}:{card_value["channelCatId"]}##{card_value["catName"]}',
                        }
                    )
                    break

        category = channel_res['data']['categoryPredictResult']
        data['itemCatDTO'] = {
            'catId': str(category['catId']),
            'catName': str(category['catName']),
            'channelCatId': str(category['channelCatId']),
            'tbCatId': str(category['tbCatId']),
        }

        location = self.get_default_location()['data']['commonAddresses'][0]
        data['itemAddrDTO'] = {
            'area': location['area'],
            'city': location['city'],
            'divisionId': location['divisionId'],
            'gps': f'{location["longitude"]},{location["latitude"]}',
            'poiId': location['poiId'],
            'poiName': location['poi'],
            'prov': location['prov'],
        }

        return self._call_mtop(api('publish'), data)

    def upload_media(self, media_path: str) -> dict[str, Any]:
        # requests 用 multipart 时会自己生成 Content-Type，这里显式去掉模板里的表单类型
        headers = {key: value for key, value in _MTOP_BASE_HEADERS.items() if key != 'Content-Type'} | {'Accept': '*/*'}
        params = {'floderId': '0', 'appkey': 'xy_chat', '_input_charset': 'utf-8'}
        with open(media_path, 'rb') as f:
            files = {'file': (Path(media_path).name, f, 'image/png')}
            return self.session.post(url=self.upload_media_url, headers=headers, files=files, params=params).json()

    def upload_image(self, media_path: str) -> ImageInfo:
        """上传本地图片，返回发布接口需要的 ImageInfo。"""
        image_object = self.upload_media(media_path)['object']
        width, height = map(int, image_object['pix'].split('x'))
        return ImageInfo(url=image_object['url'], width=width, height=height)

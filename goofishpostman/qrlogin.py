"""闲鱼扫码登录（passport 流程）。

拆成「建会话 → 取二维码 → 轮询状态 → 完成登录」四步，
每步都是独立的阻塞函数：命令行可以顺序调用，
Web 端可以用 asyncio.to_thread 分次调用，把状态跨请求保留在内存里。

注意模块名不能叫 qrcode.py，否则会遮蔽第三方 qrcode 库。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from io import BytesIO
from json import JSONDecodeError
from sys import stdout
from time import time
from typing import Any
from urllib.parse import quote

from loguru import logger
from qrcode import QRCode
from qrcode import make as make_qr_image
from requests import Session

from .cookies import Cookies
from .headers import SEC_CH_UA, USER_AGENT
from .types import MTOP_APP_KEY

_PASSPORT = 'https://passport.goofish.com'
_MINI_LOGIN_URL = f'{_PASSPORT}/mini_login.htm'
_GENERATE_URL = f'{_PASSPORT}/newlogin/qrcode/generate.do'
_QUERY_URL = f'{_PASSPORT}/newlogin/qrcode/query.do?appName=xianyu&fromSite=77'
_LOGIN_TOKEN_URL = f'{_PASSPORT}/login_token/login.do'
_MTOP_USER_NAV_URL = 'https://h5api.m.goofish.com/h5/mtop.idle.web.user.page.nav/1.0/'

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

_MTOP_HEADERS = {
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

# 生成二维码 / 轮询状态共用的固定 query
_QRCODE_PARAMS = {
    'appName': 'xianyu',
    'fromSite': '77',
    'appEntrance': 'web',
    'bizParams': f'taobaoBizLoginFrom=web&renderRefer={quote("https://www.goofish.com/")}',
    'mainPage': 'false',
    'isMobile': 'false',
    'lang': 'zh_CN',
    'returnUrl': '',
    'umidTag': 'SERVER',
}

# 轮询到的状态：NEW=待扫码 SCANNED=已扫码待确认 CONFIRMED=已确认 EXPIRED=二维码过期
STATUS_PENDING = 'PENDING'
STATUS_NEW = 'NEW'
STATUS_SCANNED = 'SCANNED'
STATUS_CONFIRMED = 'CONFIRMED'
STATUS_EXPIRED = 'EXPIRED'

STATUS_TEXT = {
    STATUS_PENDING: '正在获取二维码…',
    STATUS_NEW: '请用闲鱼 App 扫描二维码',
    STATUS_SCANNED: '已扫码，请在手机上确认登录',
    STATUS_CONFIRMED: '登录成功',
    STATUS_EXPIRED: '二维码已过期，请点击刷新',
}

# goofish / mmstat 域下需要保留的 cookie
_COOKIE_DOMAINS = ('.goofish.com', '.mmstat.com')


class QrLoginError(RuntimeError):
    """扫码登录过程中的可预期失败（过期、超时、接口异常）。"""


@dataclass
class QrSession:
    """一次扫码登录的状态，Web 端按 session_id 索引后存在内存里。"""

    session: Session
    device_id: str
    cna: str
    csrf_token: str
    status: str = STATUS_PENDING
    qr_url: str = ''
    qr_t: int = 0
    qr_ck: str = ''
    login_token: str = ''
    created_at: float = field(default_factory=time)
    # 登录成功后落库的账号 id，用于轮询接口幂等
    account_id: str = ''

    @property
    def passport_cookie2(self) -> str:
        """passport 域的 cookie2，请求里作为 hsiz 传回去。"""
        return self.session.cookies.get(name='cookie2', domain='.goofish.com') or ''

    def build_query_params(self) -> dict[str, str]:
        return _QRCODE_PARAMS | {
            '_csrf_token': self.csrf_token,
            'umidToken': '',
            'hsiz': self.passport_cookie2,
            'navlanguage': 'en',
            'navUserAgent': USER_AGENT,
            'navPlatform': 'Win32',
            'isIframe': 'true',
            'documentReferer': 'https://www.goofish.com/',
            'defaultView': 'sms',
            'deviceId': self.cna,
        }


# ── 步骤 2：取二维码 ──────────────────────────────────────────────────────────
def start_qr_login(state: QrSession) -> QrSession:
    """调用 generate.do 拿到二维码内容（codeContent）。"""
    try:
        payload = state.session.get(
            url=_GENERATE_URL,
            params=_QRCODE_PARAMS | {'_csrf_token': state.csrf_token, 'umidToken': '', 'hsiz': state.passport_cookie2},
            headers=_PASSPORT_HEADERS | {'Referer': _MINI_LOGIN_URL},
            timeout=10,
        ).json()
        data = payload['content']['data']
        state.qr_url = data['codeContent']
        state.qr_t = data['t']
        state.qr_ck = data['ck']
    except (KeyError, TypeError, JSONDecodeError, OSError) as e:
        raise QrLoginError(f'获取二维码失败: {e}') from e

    state.status = STATUS_NEW
    return state


# ── 步骤 3：轮询状态 ──────────────────────────────────────────────────────────
def poll_qr_login(state: QrSession) -> str:
    """查询一次扫码状态，并在 CONFIRMED 时记下 login_token。"""
    try:
        payload = state.session.post(
            url=_QUERY_URL,
            data={**state.build_query_params(), 't': str(state.qr_t), 'ck': state.qr_ck},
            headers=_PASSPORT_HEADERS
            | {'Content-Type': 'application/x-www-form-urlencoded', 'Origin': _PASSPORT, 'Referer': _MINI_LOGIN_URL},
            timeout=10,
        ).json()
        data = payload['content']['data']
    except (KeyError, TypeError, JSONDecodeError, OSError) as e:
        raise QrLoginError(f'查询扫码状态失败: {e}') from e

    state.status = data.get('qrCodeStatus') or STATUS_NEW
    if state.status == STATUS_CONFIRMED:
        # CONFIRMED 的响应里 Set-Cookie 已带上 sgcookie/unb/tracknick/csg
        state.login_token = data.get('token') or data.get('lgToken') or ''
    return state.status


# ── 步骤 4：完成登录 ──────────────────────────────────────────────────────────
def finish_qr_login(state: QrSession) -> dict[str, Any]:
    """完成登录并返回账号信息：{'unb', 'tracknick', 'cookie', 'device_id'}。"""
    session = state.session

    if state.login_token:
        try:
            session.post(
                url=_LOGIN_TOKEN_URL,
                params={
                    'token': state.login_token,
                    'subFlow': 'DIALOG_CHECK_LOGIN_RPC',
                    'nextCode': '0018',
                    'bizScene': 'qrcode',
                    'confirm': 'true',
                },
                data={'deviceId': state.cna},
                headers=_PASSPORT_HEADERS
                | {
                    'Content-Type': 'application/x-www-form-urlencoded',
                    'Origin': _PASSPORT,
                    'Referer': _MINI_LOGIN_URL,
                },
                timeout=10,
            )
        except OSError as e:
            raise QrLoginError(f'完成登录失败: {e}') from e

    unb = session.cookies.get('unb', domain='.goofish.com') or ''
    if not unb:
        # 拿不到 unb 说明登录态没落下来
        raise QrLoginError('登录未完成，请重新扫码')

    _refresh_mtop_cookie(session)

    return {
        'unb': unb,
        'tracknick': session.cookies.get('tracknick', domain='.goofish.com') or '',
        'cookie': build_session_cookie_string(session),
        'device_id': state.device_id,
    }


def _refresh_mtop_cookie(session: Session) -> None:
    """登录后 _m_h5_tk 会变，访问一次页面刷新它（失败不影响登录结果）。"""
    try:
        session.post(
            url=_MTOP_USER_NAV_URL,
            params={
                'jsv': '2.7.2',
                'appKey': MTOP_APP_KEY,
                't': str(int(time() * 1000)),
                'sign': '',
                'v': '1.0',
                'type': 'originaljson',
                'dataType': 'json',
                'timeout': '20000',
                'api': 'mtop.idle.web.user.page.nav',
                'sessionOption': 'AutoLoginOnly',
                'spm_cnt': 'a21ybx.home.0.0',
            },
            data={'data': '{}'},
            headers=_MTOP_HEADERS,
            timeout=10,
        )
    except OSError as e:
        logger.warning(f'刷新 mtop cookie 失败（不影响登录）: {e}')


# ── 渲染 ──────────────────────────────────────────────────────────────────────
def build_session_cookie_string(session: Session) -> str:
    """把会话里 goofish / mmstat 域的 cookie 拼成请求头用的字符串。"""
    jar = {
        cookie.name: cookie.value
        for cookie in session.cookies
        if cookie.domain and any(domain in cookie.domain for domain in _COOKIE_DOMAINS)
    }
    return str(Cookies.from_dict(jar))


def render_qr_png(qr_url: str, box_size: int = 8, border: int = 2) -> bytes:
    """把二维码内容渲染成 PNG（供网页 <img> 直接展示）。"""
    buffer = BytesIO()
    make_qr_image(qr_url, box_size=box_size, border=border).save(buffer, format='PNG')
    return buffer.getvalue()


def render_qrcode(qr_url: str) -> None:
    """终端打印二维码（用半块字符 ▀▄█ 使其接近正方形）。"""
    qr = QRCode(border=1, box_size=1)
    qr.add_data(qr_url)
    qr.make()

    matrix = qr.get_matrix()
    rows = len(matrix)
    lines = []
    for r in range(0, rows, 2):
        line = ''
        for column in range(len(matrix[r])):
            top = matrix[r][column]
            bottom = matrix[r + 1][column] if r + 1 < rows else False
            if top and bottom:
                line += '█'  # 上下都黑
            elif top:
                line += '▀'  # 上黑下白
            elif bottom:
                line += '▄'  # 上白下黑
            else:
                line += ' '  # 上下都白
        lines.append(line)
    stdout.buffer.write(('\n'.join(lines) + '\n').encode(encoding='utf-8', errors='replace'))
    stdout.buffer.flush()

"""账号配置持久化与飞书汇总推送的测试（不联网）。"""

from __future__ import annotations

from asyncio import run
from json import dumps
from os import name
from stat import S_IMODE

from httpx import HTTPError
from pytest import mark, raises

from goofishpostman.accounts import FeishuNotifier, format_message, is_response_ok
from goofishpostman.sender import build_text_payload, build_webhook_url, generate_feishu_sign
from goofishpostman.store import Store

GOOD_COOKIE = 'unb=123456; tracknick=tester; _m_h5_tk=abc_1700000000000'


# ── Store ─────────────────────────────────────────────────────────────────────
def test_store_add_and_reload(tmp_dir) -> None:
    path = tmp_dir / 'accounts.json'
    store = Store(path)
    account = store.add(name='主力号', cookie=GOOD_COOKIE)

    reloaded = Store(path)
    assert [a.id for a in reloaded.list_accounts()] == [account.id]
    assert reloaded.get(account.id).cookie == GOOD_COOKIE
    assert reloaded.get(account.id).name == '主力号'


def test_store_update_and_remove(tmp_dir) -> None:
    store = Store(tmp_dir / 'a.json')
    account = store.add(name='旧名', cookie=GOOD_COOKIE, enabled=True)

    updated = store.update(account.id, name='新名', enabled=False)
    assert (updated.name, updated.enabled) == ('新名', False)
    assert updated.updated_at >= account.updated_at

    store.remove(account.id)
    assert store.list_accounts() == []
    with raises(KeyError):
        store.remove(account.id)


def test_store_update_ignores_none(tmp_dir) -> None:
    store = Store(tmp_dir / 'a.json')
    account = store.add(name='名字', cookie=GOOD_COOKIE)
    assert store.update(account.id, name=None, enabled=None).name == '名字'


def test_account_timestamps_are_timezone_aware(tmp_dir) -> None:
    """新建/更新都用带时区的 UTC：naive 与 aware 混在一起比较会抛 TypeError。"""
    path = tmp_dir / 'aware.json'
    store = Store(path)
    account = store.add(name='号', cookie=GOOD_COOKIE)
    assert account.created_at.tzinfo is not None
    assert account.updated_at.tzinfo is not None

    updated = store.update(account.id, name='新名')
    assert updated.updated_at >= account.created_at
    assert Store(path).get(account.id).updated_at.tzinfo is not None


def test_legacy_config_without_timezone_still_works(tmp_dir) -> None:
    """老配置文件里的时间不带时区：要能照常读取、展示、修改。"""
    path = tmp_dir / 'legacy.json'
    path.write_text(
        dumps(
            {
                'accounts': [
                    {
                        'id': 'old1',
                        'name': '老账号',
                        'cookie': GOOD_COOKIE,
                        'enabled': True,
                        'nickname_override': '',
                        'created_at': '2026-09-01T10:00:00',
                        'updated_at': '2026-09-01T10:00:00',
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding='utf-8',
    )

    store = Store(path)
    assert store.get('old1').to_public()['updated_at'] == '2026-09-01T10:00:00'
    updated = store.update('old1', name='改过的老账号')
    assert updated.name == '改过的老账号'
    assert updated.updated_at.tzinfo is not None


@mark.skipif(name != 'posix', reason='Windows 不使用 POSIX 权限位')
def test_store_file_is_private(tmp_dir) -> None:
    store = Store(tmp_dir / 'a.json')
    store.add(name='x', cookie=GOOD_COOKIE)
    assert S_IMODE(store.path.stat().st_mode) == 0o600


def test_store_rejects_broken_json(tmp_dir) -> None:
    path = tmp_dir / 'a.json'
    path.write_text('{not json', encoding='utf-8')
    with raises(RuntimeError, match='损坏'):
        Store(path)


def test_account_cookie_validation(tmp_dir) -> None:
    store = Store(tmp_dir / 'a.json')
    good = store.add(name='good', cookie=GOOD_COOKIE)
    assert good.find_missing_cookie_keys() == []
    assert good.is_usable()

    bad = store.add(name='bad', cookie='unb=1')
    assert set(bad.find_missing_cookie_keys()) == {'tracknick', '_m_h5_tk'}
    assert not bad.is_usable()

    disabled = store.add(name='off', cookie=GOOD_COOKIE, enabled=False)
    assert not disabled.is_usable()


def test_to_public_never_leaks_cookie(tmp_dir) -> None:
    store = Store(tmp_dir / 'a.json')
    account = store.add(name='x', cookie=GOOD_COOKIE)
    public = account.to_public()
    assert 'cookie' not in public
    assert public['has_cookie'] is True
    assert public['cookie_hint'] == '***0000'
    assert 'unb=123456' not in str(public)


def test_display_name_fallback(tmp_dir) -> None:
    store = Store(tmp_dir / 'a.json')
    assert store.add(name='  ', cookie=GOOD_COOKIE).display_name == '未命名账号'


def test_notify_settings_roundtrip(tmp_dir) -> None:
    store = Store(tmp_dir / 'a.json')
    store.update_notify(uuid='uuid-1', secret='s3cret')
    assert store.data.notify.configured

    reloaded = Store(store.path)
    assert reloaded.data.notify.uuid == 'uuid-1'
    assert reloaded.data.notify.secret == 's3cret'


# ── 飞书推送 ──────────────────────────────────────────────────────────────────
def test_build_webhook_url_strips_spaces() -> None:
    assert build_webhook_url(' abc ') == 'https://open.feishu.cn/open-apis/bot/v2/hook/abc'


def test_generate_feishu_sign_matches_manual_hmac() -> None:
    from base64 import b64encode
    from hashlib import sha256
    from hmac import new

    expected = b64encode(new(key=b'1700000000\nsecret', digestmod=sha256).digest()).decode()
    assert generate_feishu_sign('secret', 1700000000) == expected


def test_build_text_payload_signs_only_with_secret() -> None:
    plain = build_text_payload('hi')
    assert plain == {'msg_type': 'text', 'content': {'text': 'hi'}}

    signed = build_text_payload('hi', 'secret')
    assert signed['sign'] and signed['timestamp']


@mark.parametrize(
    ('body', 'expected'),
    [
        ({'code': 0, 'msg': 'success'}, True),
        ({'code': 19021, 'msg': 'sign match fail'}, False),
        ({'StatusCode': 0}, True),
        ({'StatusCode': 1}, False),
        ({}, False),
        ({'msg': 'nonsense'}, False),
    ],
)
def test_is_response_ok(body: dict, expected: bool) -> None:
    assert is_response_ok(body) is expected


def test_format_message_carries_account_and_sender() -> None:
    """文案不带【】前缀，接收方写成「昵称(账号)」。"""
    info = {'send_user_name': '买家小王', 'send_message': '在吗'}
    text = format_message('小号(xy773249480508)', info, '在吗')
    assert text == '买家小王 → 小号(xy773249480508)\n在吗'
    assert '【' not in text and '】' not in text


def test_format_message_marks_direction_for_account_to_account() -> None:
    """账号之间互发时，也要能看出是谁发给谁的。"""
    info = {'send_user_name': '大号', 'send_message': '在吗'}
    other = format_message('小号(222)', info, '在吗')
    assert other == '大号 → 小号(222)\n在吗'
    assert other != format_message('大号(111)', info, '在吗')  # 接收方不同，文案不同


def test_format_message_prefers_resolved_sender() -> None:
    """调用方解析出的发送方昵称优先于报文原始值。"""
    info = {'send_user_name': '13993122', 'send_message': 'hi'}
    assert format_message('小号(222)', info, 'hi', sender='网课学习私人助理') == ('网课学习私人助理 → 小号(222)\nhi')


def test_format_message_falls_back_when_sender_unknown() -> None:
    info = {'send_user_name': '', 'send_user_id': '12345', 'send_message': 'hi'}
    assert format_message('小号(222)', info, 'hi') == '12345 → 小号(222)\nhi'


def test_notifier_without_uuid_is_noop() -> None:
    notifier = FeishuNotifier()
    assert not notifier.configured
    # 未配置时不应抛错，也不应发起请求
    run(notifier.send('hello'))


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.body = body

    def json(self) -> dict:
        return self.body


class FakeClient:
    """仿 httpx.AsyncClient 的最小接口。"""

    def __init__(self, body: dict) -> None:
        self.body = body
        self.is_closed = False
        self.calls: list[dict] = []

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append({'url': url, **kwargs})
        return FakeResponse(self.body)

    async def aclose(self) -> None:
        self.is_closed = True


def test_notifier_raises_on_error_code(monkeypatch) -> None:
    notifier = FeishuNotifier(uuid='abc', secret='secret')
    monkeypatch.setattr(notifier, '_client', FakeClient({'code': 19021, 'msg': 'sign match fail'}))
    with raises(Exception, match='sign match fail'):
        run(notifier.send('hi'))


def test_notifier_posts_card_with_details_code_block(monkeypatch) -> None:
    """私信走富文本卡片：标题写流向，明细进代码框，正文是普通文本。"""
    client = FakeClient({'code': 0, 'msg': 'success'})
    notifier = FeishuNotifier(uuid='abc')
    monkeypatch.setattr(notifier, '_client', client)
    run(
        notifier.send_account_message(
            '主力号',
            {'send_user_name': '买家'},
            '在吗\n第一行\n\n第三行',
            details={'时间': '09-11 23:55:32', '商品': '玲娜贝儿钱包'},
        )
    )

    call = client.calls[0]
    assert call['url'] == 'https://open.feishu.cn/open-apis/bot/v2/hook/abc'
    payload = call['json']
    assert payload['msg_type'] == 'interactive'
    card = payload['card']
    assert card['schema'] == '2.0'
    assert card['header']['title']['content'] == '买家 → 主力号'
    body = next(call for call in client.calls if call['url'].endswith('/bot/v2/hook/abc'))['json']['card']['body']
    # 一个属性一行；内嵌成功后连正文里那句 `[图片]` 标注也去掉（图片就在眼前）
    assert body['elements'][0] == {
        'tag': 'markdown',
        'content': '**时间** 09-11 23:55:32\n**商品** 玲娜贝儿钱包',
        'text_size': 'notation',
    }
    # 明细与正文之间有分割线；正文的换行与空行原样保留
    assert body['elements'][1] == {'tag': 'hr'}
    assert body['elements'][2] == {'tag': 'markdown', 'content': '在吗\n第一行\n\n第三行'}


def test_notifier_card_is_signed_when_secret_given(monkeypatch) -> None:
    client = FakeClient({'code': 0, 'msg': 'success'})
    notifier = FeishuNotifier(uuid='abc', secret='s3cret')
    monkeypatch.setattr(notifier, '_client', client)
    run(notifier.send_account_message('主力号', {'send_user_name': '买家'}, '在吗'))

    payload = client.calls[0]['json']
    assert payload['msg_type'] == 'interactive'
    assert payload['timestamp'] and payload['sign']


def test_notifier_send_keeps_plain_text(monkeypatch) -> None:
    """告警之类的纯文本推送不受影响。"""
    client = FakeClient({'code': 0, 'msg': 'success'})
    notifier = FeishuNotifier(uuid='abc')
    monkeypatch.setattr(notifier, '_client', client)
    run(notifier.send('出问题了'))

    payload = client.calls[0]['json']
    assert payload['msg_type'] == 'text'
    assert payload['content']['text'] == '出问题了'


# ── 图片内嵌（自建应用上传换 image_key）────────────────────────────────────────
IMAGE_URL = 'https://img.alicdn.com/imgextra/i4/O1CN01RSAPMB1dNBgdIEGcB_!!53-xy_chat.heic'


class FakeDownload:
    """仿 httpx.Response 的下载结果。"""

    def __init__(self, content: bytes) -> None:
        self.content = content

    def raise_for_status(self) -> None:
        return None


class FakeImageClient:
    """按 URL 区分「换 token / 传图片 / 发 webhook」三类请求。"""

    def __init__(self, upload_code: int = 0) -> None:
        self.upload_code = upload_code
        self.is_closed = False
        self.calls: list[dict] = []
        self.image = b'\xff\xd8\xff\xe0fake-jpeg'

    async def get(self, url: str) -> FakeDownload:
        self.calls.append({'method': 'GET', 'url': url})
        return FakeDownload(self.image)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append({'method': 'POST', 'url': url, **kwargs})
        if url.endswith('/tenant_access_token/internal'):
            return FakeResponse({'code': 0, 'tenant_access_token': 'tok-1', 'expire': 7200})
        if url.endswith('/im/v1/images'):
            if self.upload_code:
                return FakeResponse({'code': self.upload_code, 'msg': 'upload denied'})
            return FakeResponse({'code': 0, 'data': {'image_key': 'img-key-1'}})
        return FakeResponse({'code': 0, 'msg': 'success'})

    async def aclose(self) -> None:
        self.is_closed = True

    def count(self, suffix: str) -> int:
        return sum(1 for call in self.calls if call['url'].endswith(suffix))


def make_image_notifier(monkeypatch, client: FakeImageClient, *, app: bool = True) -> FeishuNotifier:
    """假客户端要挂到两个客户端上：webhook 用 _client，下载/上传用 _media_client。

    （上传必须走后者：前者带 `Content-Type: application/json` 默认头，飞书会报 234001。）
    """
    notifier = FeishuNotifier(uuid='abc', app_id='cli_1' if app else '', app_secret='appsecret' if app else '')
    monkeypatch.setattr(notifier, '_client', client)
    monkeypatch.setattr(notifier, '_media_client', client)
    return notifier


def test_image_is_inlined_with_the_uploaded_key(monkeypatch) -> None:
    """配了自建应用：图片地址先上传换 image_key，卡片正文里直接显示图片。"""
    client = FakeImageClient()
    notifier = make_image_notifier(monkeypatch, client)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    assert client.count('/tenant_access_token/internal') == 1
    assert client.count('/im/v1/images') == 1
    upload = next(call for call in client.calls if call['url'].endswith('/im/v1/images'))
    assert upload['headers']['Authorization'] == 'Bearer tok-1'
    assert upload['data'] == {'image_type': 'message'}

    body = next(call for call in client.calls if call['url'].endswith('/bot/v2/hook/abc'))['json']['card']['body']
    # 标注行与地址行都被内嵌图片取代，正文里只剩图片本身
    assert body['elements'][0] == {'tag': 'markdown', 'content': '![图片](img-key-1)'}
    assert IMAGE_URL not in str(body)
    assert '[图片]' not in body['elements'][0]['content'].splitlines()  # 没有单独的标注行


def test_image_keeps_link_without_app_credentials(monkeypatch) -> None:
    """没配自建应用：不上传，正文里保留可点开的地址（当前默认行为）。"""
    client = FakeImageClient()
    notifier = make_image_notifier(monkeypatch, client, app=False)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    assert client.count('/im/v1/images') == 0
    body = next(call for call in client.calls if call['url'].endswith('/bot/v2/hook/abc'))['json']['card']['body']
    assert body['elements'][0]['content'] == f'[图片]\n[{IMAGE_URL}]({IMAGE_URL})'


def test_upload_failure_falls_back_to_the_link(monkeypatch) -> None:
    """上传失败不能把整条推送带崩，退回链接即可。"""
    client = FakeImageClient(upload_code=234001)
    notifier = make_image_notifier(monkeypatch, client)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    body = next(call for call in client.calls if call['url'].endswith('/bot/v2/hook/abc'))['json']['card']['body']
    assert body['elements'][0]['content'] == f'[图片]\n[{IMAGE_URL}]({IMAGE_URL})'


def test_same_image_is_uploaded_once(monkeypatch) -> None:
    """同一张图（含重复下发的同一条消息）只上传一次。"""
    client = FakeImageClient()
    notifier = make_image_notifier(monkeypatch, client)
    for _ in range(3):
        run(
            notifier.send_account_message(
                '主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,)
            )
        )

    assert client.count('/im/v1/images') == 1
    assert client.count('O1CN01RSAPMB1dNBgdIEGcB_!!53-xy_chat.heic') == 1  # 也只下载一次


def test_download_failure_falls_back_to_the_link(monkeypatch) -> None:
    """图片下载不到（地址过期等）同样退回链接。"""

    class FailingClient(FakeImageClient):
        async def get(self, url: str) -> FakeDownload:
            self.calls.append({'method': 'GET', 'url': url})
            raise HTTPError('boom')

    client = FailingClient()
    notifier = make_image_notifier(monkeypatch, client)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    assert client.count('/im/v1/images') == 0
    body = next(call for call in client.calls if call['url'].endswith('/bot/v2/hook/abc'))['json']['card']['body']
    assert f'[{IMAGE_URL}]({IMAGE_URL})' in body['elements'][0]['content']

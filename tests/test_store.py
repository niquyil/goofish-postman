"""账号配置持久化与飞书汇总推送的测试（不联网）。"""

from __future__ import annotations

from asyncio import run
from json import dumps, loads
from os import name
from stat import S_IMODE

from httpx import HTTPError
from pytest import mark, raises

from goofishpostman.accounts import FeishuNotifier, format_direction
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
    store.update_notify(app_id='cli_1', app_secret='s3cret', chat_id='oc_1')
    assert store.data.notify.configured

    reloaded = Store(store.path)
    assert reloaded.data.notify.app_id == 'cli_1'
    assert reloaded.data.notify.app_secret == 's3cret'
    assert reloaded.data.notify.chat_id == 'oc_1'


def test_notify_is_not_configured_without_target_chat(tmp_dir) -> None:
    """只有应用凭据、没选目标群时不算配好（消息发不出去）。"""
    store = Store(tmp_dir / 'a.json')
    store.update_notify(app_id='cli_1', app_secret='s3cret')
    assert store.data.notify.has_app_credentials is True
    assert store.data.notify.configured is False


# ── 飞书推送 ──────────────────────────────────────────────────────────────────
def test_format_direction_carries_sender_and_receiver() -> None:
    """卡片标题的文案：发送方 → 接收方，不带【】前缀。"""
    info = {'send_user_name': '买家小王', 'send_message': '在吗'}
    title = format_direction('小号(xy773249480508)', info)
    assert title == '买家小王 → 小号(xy773249480508)'
    assert '【' not in title and '】' not in title


def test_format_direction_prefers_resolved_sender() -> None:
    """调用方解析出的发送方昵称优先于报文原始值。"""
    info = {'send_user_name': '13993122', 'send_message': 'hi'}
    assert format_direction('小号(222)', info, sender='网课学习私人助理') == '网课学习私人助理 → 小号(222)'


def test_format_direction_falls_back_when_sender_unknown() -> None:
    info = {'send_user_name': '', 'send_user_id': '12345', 'send_message': 'hi'}
    assert format_direction('小号(222)', info) == '12345 → 小号(222)'


def test_notifier_without_credentials_is_noop() -> None:
    notifier = FeishuNotifier()
    assert not notifier.can_send_via_app
    # 未配置时不应抛错，也不应发起请求
    run(notifier.send('hello'))


class FakeResponse:
    def __init__(self, body: dict) -> None:
        self.body = body

    def json(self) -> dict:
        return self.body


class FakeClient:
    """仿 httpx.AsyncClient 的最小接口（只用于「应用接口返回错误码」这类用例）。"""

    def __init__(self, body: dict) -> None:
        self.body = body
        self.is_closed = False
        self.calls: list[dict] = []

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append({'url': url, **kwargs})
        if url.endswith('/tenant_access_token/internal'):
            return FakeResponse({'code': 0, 'tenant_access_token': 'tok-1', 'expire': 7200})
        return FakeResponse(self.body)

    async def aclose(self) -> None:
        self.is_closed = True


def test_notifier_raises_on_error_code(monkeypatch) -> None:
    notifier = FeishuNotifier(app_id='cli_1', app_secret='sec', chat_id='oc_1')
    monkeypatch.setattr(notifier, '_client', FakeClient({'code': 230002, 'msg': 'bot not in chat'}))
    with raises(Exception, match='bot not in chat'):
        run(notifier.send('hi'))


def test_notifier_card_layout(monkeypatch) -> None:
    """私信走富文本卡片：标题写流向，明细是多行小号灰字 + 分割线，正文是普通文本。"""
    client = FakeImageClient()
    notifier = FeishuNotifier(app_id='cli_1', app_secret='sec', chat_id='oc_1')
    monkeypatch.setattr(notifier, '_client', client)
    monkeypatch.setattr(notifier, '_media_client', client)
    run(
        notifier.send_account_message(
            '主力号',
            {'send_user_name': '买家'},
            '在吗\n第一行\n\n第三行',
            details={'时间': '09-11 23:55:32', '商品': '玲娜贝儿钱包'},
        )
    )

    card = card_of(client.last('/im/v1/messages'))
    assert card['schema'] == '2.0'
    assert card['header']['title']['content'] == '买家 → 主力号'
    body = card['body']
    # 一个属性一行
    assert body['elements'][0] == {
        'tag': 'markdown',
        'content': '**时间** 09-11 23:55:32\n**商品** 玲娜贝儿钱包',
        'text_size': 'notation',
    }
    # 明细与正文之间有分割线；正文的换行与空行原样保留
    assert body['elements'][1] == {'tag': 'hr'}
    assert body['elements'][2] == {'tag': 'markdown', 'content': '在吗\n第一行\n\n第三行'}


# ── 图片内嵌（自建应用上传换 image_key）────────────────────────────────────────
IMAGE_URL = 'https://img.alicdn.com/imgextra/i4/O1CN01RSAPMB1dNBgdIEGcB_!!53-xy_chat.heic'


class FakeDownload:
    """仿 httpx.Response：既能当图片下载结果，也能当列群接口的返回。"""

    def __init__(self, content: bytes, body: dict | None = None, status_code: int = 200) -> None:
        self.content = content
        self.body = body or {}
        self.status_code = status_code

    def json(self) -> dict:
        return self.body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f'HTTP {self.status_code}')


class FakeImageClient:
    """按 URL 区分「换 token / 传图片 / 发消息 / 列群」几类请求。"""

    def __init__(self, upload_code: int = 0, message_code: int = 0, download_status: int = 200) -> None:
        self.upload_code = upload_code
        self.message_code = message_code
        self.download_status = download_status
        self.is_closed = False
        self.calls: list[dict] = []
        self.image = b'\xff\xd8\xff\xe0fake-jpeg'

    async def get(self, url: str, **kwargs) -> FakeDownload:
        self.calls.append({'method': 'GET', 'url': url, **kwargs})
        if url.endswith('/im/v1/chats'):
            return FakeDownload(b'', {'code': 0, 'data': {'items': [{'chat_id': 'oc_chat1', 'name': '卖家消息'}]}})
        return FakeDownload(self.image, status_code=self.download_status)

    async def post(self, url: str, **kwargs) -> FakeResponse:
        self.calls.append({'method': 'POST', 'url': url, **kwargs})
        if url.endswith('/tenant_access_token/internal'):
            return FakeResponse({'code': 0, 'tenant_access_token': 'tok-1', 'expire': 7200})
        if url.endswith('/im/v1/images'):
            if self.upload_code:
                return FakeResponse({'code': self.upload_code, 'msg': 'upload denied'})
            return FakeResponse({'code': 0, 'data': {'image_key': 'img-key-1'}})
        if url.endswith('/im/v1/messages'):
            if self.message_code:
                return FakeResponse({'code': self.message_code, 'msg': 'send denied'})
            return FakeResponse({'code': 0, 'data': {'message_id': 'om_1'}, 'msg': 'success'})
        return FakeResponse({'code': 0, 'msg': 'success'})

    async def aclose(self) -> None:
        self.is_closed = True

    def count(self, suffix: str) -> int:
        return sum(1 for call in self.calls if call['url'].endswith(suffix))

    def last(self, suffix: str) -> dict:
        return next(call for call in reversed(self.calls) if call['url'].endswith(suffix))


def make_image_notifier(monkeypatch, client: FakeImageClient, *, app: bool = True) -> FeishuNotifier:
    """假客户端要挂到两个客户端上：发消息用 _client，下载/上传用 _media_client。

    （上传必须走后者：前者带 `Content-Type: application/json` 默认头，飞书会报 234001。）
    """
    notifier = FeishuNotifier(
        app_id='cli_1' if app else '', app_secret='appsecret' if app else '', chat_id='oc_chat1' if app else ''
    )
    monkeypatch.setattr(notifier, '_client', client)
    monkeypatch.setattr(notifier, '_media_client', client)
    return notifier


def card_of(call: dict) -> dict:
    """从自建应用发消息的请求里取出卡片对象（content 是 JSON 字符串）。"""
    return loads(call['json']['content'])


def test_message_is_sent_by_the_app(monkeypatch) -> None:
    """配好应用 + 目标群：走机器人应用接口发卡片。

    - 接口：POST /open-apis/im/v1/messages?receive_id_type=chat_id
    - content 是 JSON 字符串，也没有 timestamp/sign（签名只属于自定义机器人）
    """
    client = FakeImageClient()
    notifier = make_image_notifier(monkeypatch, client)
    run(notifier.send_account_message('主力号', {'send_user_name': '买家'}, '在吗', details={'时间': '09-12 22:30:00'}))

    call = client.last('/im/v1/messages')
    assert call['params'] == {'receive_id_type': 'chat_id'}
    assert call['headers']['Authorization'] == 'Bearer tok-1'
    assert call['json']['receive_id'] == 'oc_chat1'
    assert call['json']['msg_type'] == 'interactive'
    body = card_of(call)
    assert body['schema'] == '2.0'
    assert body['header']['title']['content'] == '买家 → 主力号'
    assert body['body']['elements'][0]['content'] == '**时间** 09-12 22:30:00'
    assert body['body']['elements'][1] == {'tag': 'hr'}
    assert 'sign' not in call['json'] and 'timestamp' not in call['json']


def test_plain_text_is_sent_as_text_message(monkeypatch) -> None:
    """告警文本（send）走 text 消息类型。"""
    client = FakeImageClient()
    notifier = make_image_notifier(monkeypatch, client)
    run(notifier.send('出问题了'))

    call = client.last('/im/v1/messages')
    assert call['json']['msg_type'] == 'text'
    assert loads(call['json']['content']) == {'text': '出问题了'}


def test_nothing_is_sent_without_target_chat(monkeypatch) -> None:
    """只填了应用凭据、没选目标群时不发送（也不报错）。"""
    client = FakeImageClient()
    notifier = make_image_notifier(monkeypatch, client, app=False)
    run(notifier.send_account_message('主力号', {'send_user_name': '买家'}, '在吗'))

    assert client.calls == []


def test_app_send_failure_raises_notify_error(monkeypatch) -> None:
    """应用接口报错要抛 NotifyError（Supervisor 会记事件，不会静默丢消息）。"""
    from goofishpostman.accounts import NotifyError

    client = FakeImageClient(message_code=230002)
    notifier = make_image_notifier(monkeypatch, client)
    with raises(NotifyError, match='230002'):
        run(notifier.send('在吗'))


def test_image_is_inlined_with_the_uploaded_key(monkeypatch) -> None:
    """配了自建应用：图片地址先上传换 image_key，卡片正文里直接显示图片。"""
    client = FakeImageClient()
    notifier = make_image_notifier(monkeypatch, client)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    assert client.count('/tenant_access_token/internal') == 1
    assert client.count('/im/v1/images') == 1
    upload = client.last('/im/v1/images')
    assert upload['headers']['Authorization'] == 'Bearer tok-1'
    assert upload['data'] == {'image_type': 'message'}

    elements = card_of(client.last('/im/v1/messages'))['body']['elements']
    # 标注行与地址行都被内嵌图片取代，正文里只剩图片本身
    assert elements[0] == {'tag': 'markdown', 'content': '![图片](img-key-1)'}
    assert IMAGE_URL not in str(elements)
    assert '[图片]' not in elements[0]['content'].splitlines()  # 没有单独的标注行


def test_image_keeps_link_when_download_not_possible(monkeypatch) -> None:
    """图片下载不到时（例如地址过期）正文里保留可点开的地址，消息照发。"""
    client = FakeImageClient(download_status=420)
    notifier = make_image_notifier(monkeypatch, client)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    assert client.count('/im/v1/images') == 0
    elements = card_of(client.last('/im/v1/messages'))['body']['elements']
    assert elements[0]['content'] == f'[图片]\n[{IMAGE_URL}]({IMAGE_URL})'


def test_upload_failure_falls_back_to_the_link(monkeypatch) -> None:
    """上传失败不能把整条推送带崩，退回链接即可。"""
    client = FakeImageClient(upload_code=234001)
    notifier = make_image_notifier(monkeypatch, client)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    elements = card_of(client.last('/im/v1/messages'))['body']['elements']
    assert elements[0]['content'] == f'[图片]\n[{IMAGE_URL}]({IMAGE_URL})'


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
        async def get(self, url: str, **kwargs) -> FakeDownload:
            self.calls.append({'method': 'GET', 'url': url})
            raise HTTPError('boom')

    client = FailingClient()
    notifier = make_image_notifier(monkeypatch, client)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    assert client.count('/im/v1/images') == 0
    elements = card_of(client.last('/im/v1/messages'))['body']['elements']
    assert f'[{IMAGE_URL}]({IMAGE_URL})' in elements[0]['content']


def test_list_chats_returns_groups(monkeypatch) -> None:
    """列群接口用于界面上挑推送目标（需要 im:chat:readonly 权限）。"""
    client = FakeImageClient()
    notifier = make_image_notifier(monkeypatch, client)
    chats = run(notifier.list_chats())

    assert chats == [{'chat_id': 'oc_chat1', 'name': '卖家消息'}]
    assert client.last('/im/v1/chats')['headers']['Authorization'] == 'Bearer tok-1'

"""账号配置持久化与飞书汇总推送的测试（不联网）。"""

from __future__ import annotations

from asyncio import run
from json import dumps, loads
from os import name
from stat import S_IMODE
from types import SimpleNamespace

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


def test_notify_chat_list_is_cached_on_disk(tmp_dir) -> None:
    """群列表缓存要落盘：重启进程后重开页面也不用再调飞书接口。"""
    store = Store(tmp_dir / 'a.json')
    store.update_notify(app_id='cli_1', app_secret='s3cret', chat_id='oc_1')
    assert store.data.notify.chats == []
    assert '群列表还没缓存' in store.data.notify.chats_hint

    chats = [
        {'chat_id': 'oc_1', 'name': '闲鱼消息汇总'},
        {'chat_id': 'oc_2', 'name': ''},
        {'chat_id': '', 'name': '脏数据'},  # 没有群 id 的条目直接丢掉
    ]
    notify = store.update_notify_chats(chats)
    assert [chat.chat_id for chat in notify.chats] == ['oc_1', 'oc_2']
    assert notify.chat_name == '闲鱼消息汇总'
    assert notify.chats_fetched_at is not None

    reloaded = Store(store.path).data.notify
    assert [chat.name for chat in reloaded.chats] == ['闲鱼消息汇总', '']
    # 缓存命中时不再提示去拉群列表，而是说明缓存时间与条数
    assert reloaded.chats_hint.startswith('群列表缓存于 ')
    assert '共 2 个' in reloaded.chats_hint

    # 界面/接口拿到的快照：群名缺失时 label 只给 id（避免「oc_2（oc_2）」），且不带密钥
    public = reloaded.to_public()
    assert public['chats'][0] == {'chat_id': 'oc_1', 'name': '闲鱼消息汇总', 'label': '闲鱼消息汇总（oc_1）'}
    assert public['chats'][1]['label'] == 'oc_2'
    assert public['chat_name'] == '闲鱼消息汇总'
    assert 's3cret' not in str(public)


def test_notify_chat_cache_is_bounded_and_app_id_change_clears_it(tmp_dir) -> None:
    """缓存条数要有上限；换了应用后旧缓存作废（旧应用能看到的群对新应用不一定有效）。"""
    store = Store(tmp_dir / 'a.json')
    store.update_notify(app_id='cli_1', app_secret='s3cret')
    store.update_notify_chats([{'chat_id': f'oc_{i}', 'name': f'群{i}'} for i in range(260)])
    assert len(store.data.notify.chats) == 200

    store.update_notify(app_id='cli_2')  # 换应用
    assert store.data.notify.chats == []
    assert store.data.notify.chats_fetched_at is None

    store.update_notify_chats([{'chat_id': 'oc_9', 'name': '群9'}])
    store.update_notify(app_id='cli_2', chat_id='oc_9')  # 同一个应用，缓存保留
    assert [chat.chat_id for chat in store.data.notify.chats] == ['oc_9']


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


class FakeLarkResponse:
    """仿 lark_oapi 的响应对象：success() / code / msg / data。"""

    def __init__(self, code: int = 0, msg: str = 'success', data=None) -> None:
        self.code = code
        self.msg = msg
        self.data = data

    def success(self) -> bool:
        return self.code == 0

    def get_log_id(self) -> str:
        return 'log-1'


class FakeLarkResource:
    """仿 lark_oapi 的 im.v1.message / image / chat 资源：只记录收到的 request。"""

    def __init__(self, owner: FakeLarkClient, name: str) -> None:
        self.owner = owner
        self.name = name

    def create(self, request) -> FakeLarkResponse:
        self.owner.calls.append((self.name, request))
        return self.owner.build_response(self.name)

    def list(self, request) -> FakeLarkResponse:
        self.owner.calls.append((self.name, request))
        return self.owner.build_response(self.name)


class FakeLarkClient:
    """仿 lark_oapi.Client 的最小接口（只用到 im.v1.message / image / file / chat）。"""

    def __init__(self, message_code: int = 0, image_code: int = 0, chat_code: int = 0, file_code: int = 0) -> None:
        self.message_code = message_code
        self.image_code = image_code
        self.chat_code = chat_code
        self.file_code = file_code
        self.calls: list[tuple[str, object]] = []
        v1 = SimpleNamespace(
            message=FakeLarkResource(self, 'message'),
            image=FakeLarkResource(self, 'image'),
            file=FakeLarkResource(self, 'file'),
            chat=FakeLarkResource(self, 'chat'),
        )
        self.im = SimpleNamespace(v1=v1)

    def build_response(self, name: str) -> FakeLarkResponse:
        match name:
            case 'message':
                return FakeLarkResponse(self.message_code, 'send denied', SimpleNamespace(message_id='om_1'))
            case 'image':
                return FakeLarkResponse(self.image_code, 'upload denied', SimpleNamespace(image_key='img-key-1'))
            case 'file':
                return FakeLarkResponse(self.file_code, 'file upload denied', SimpleNamespace(file_key='file-key-1'))
            case _:
                items = [SimpleNamespace(chat_id='oc_chat1', name='卖家消息')]
                return FakeLarkResponse(self.chat_code, 'list denied', SimpleNamespace(items=items))

    def count(self, name: str) -> int:
        return sum(1 for resource, _ in self.calls if resource == name)

    def last(self, name: str):
        return next(request for resource, request in reversed(self.calls) if resource == name)


def test_message_links_round_trip_and_are_bounded(tmp_dir) -> None:
    """飞书消息 → 闲鱼会话 的对照表要能落盘复用，并且不会无限增长。"""
    from goofishpostman.store import _MAX_MESSAGE_LINKS, MessageLink

    store = Store(tmp_dir / 'a.json')
    store.remember_message_link('om-1', 'acct-1', '54995284239', '2221114099805', '买家小王')

    reloaded = Store(store.path)
    link = reloaded.get_message_link('om-1')
    assert link is not None
    assert (link.account_id, link.cid, link.toid) == ('acct-1', '54995284239', '2221114099805')
    assert link.peer_name == '买家小王'  # 回复反馈里要写「向 买家小王 回复」

    # 直接把表填满（真写 500 次文件在 Windows 上又慢又容易被占用），再记一条看是否淘汰最早的
    store.data.message_links = {
        f'om-seed{index}': MessageLink(account_id='acct-1', cid='cid', toid='toid')
        for index in range(_MAX_MESSAGE_LINKS)
    }
    store.remember_message_link('om-new', 'acct-1', 'cid', 'toid')
    assert len(store.data.message_links) == _MAX_MESSAGE_LINKS
    assert store.get_message_link('om-1') is None  # 最早的被挤掉了
    assert store.get_message_link('om-new') is not None
    assert store.get_message_link('') is None


# ── 图片内嵌（上传换 image_key）────────────────────────────────────────────────
IMAGE_URL = 'https://img.alicdn.com/imgextra/i4/O1CN01RSAPMB1dNBgdIEGcB_!!53-xy_chat.heic'


class FakeDownload:
    """仿 httpx.Response：图片下载结果。"""

    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.content = content
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise HTTPError(f'HTTP {self.status_code}')


class FakeDownloadClient:
    """仿 httpx.AsyncClient，只管下载闲鱼图片 / 媒体。"""

    def __init__(self, status_code: int = 200, image: bytes | None = None) -> None:
        self.status_code = status_code
        self.is_closed = False
        self.urls: list[str] = []
        self.image = image if image is not None else b'\xff\xd8\xff\xe0fake-jpeg'

    async def get(self, url: str, **kwargs) -> FakeDownload:
        self.urls.append(url)
        if self.status_code >= 400:
            raise HTTPError(f'HTTP {self.status_code}')
        return FakeDownload(self.image, status_code=self.status_code)

    async def aclose(self) -> None:
        self.is_closed = True


def make_notifier(monkeypatch, lark: FakeLarkClient, downloads: FakeDownloadClient | None = None, *, app: bool = True):
    """把假的 SDK 客户端与下载客户端挂到 notifier 上。"""
    notifier = FeishuNotifier(
        app_id='cli_1' if app else '', app_secret='appsecret' if app else '', chat_id='oc_chat1' if app else ''
    )
    monkeypatch.setattr(notifier, '_client', lark)
    monkeypatch.setattr(notifier, '_media_client', downloads or FakeDownloadClient())
    return notifier


def card_of(request) -> dict:
    """从 SDK 的发消息请求里取出卡片对象（content 是 JSON 字符串）。"""
    return loads(request.body.content)


def test_message_is_sent_through_the_sdk(monkeypatch) -> None:
    """发消息走 lark-oapi 的 im.v1.message.create，receive_id_type=chat_id。"""
    lark = FakeLarkClient()
    notifier = make_notifier(monkeypatch, lark)
    run(notifier.send_account_message('主力号', {'send_user_name': '买家'}, '在吗', details={'时间': '09-12 22:30:00'}))

    request = lark.last('message')
    assert request.uri == '/open-apis/im/v1/messages'
    assert ('receive_id_type', 'chat_id') in request.queries
    assert request.body.receive_id == 'oc_chat1'
    assert request.body.msg_type == 'interactive'
    card = card_of(request)
    assert card['schema'] == '2.0'
    assert card['header']['title']['content'] == '买家 → 主力号'
    assert card['body']['elements'][0]['content'] == '**时间** 09-12 22:30:00'
    assert card['body']['elements'][1] == {'tag': 'hr'}


def test_plain_text_is_sent_as_text_message(monkeypatch) -> None:
    """告警文本（send）走 text 消息类型。"""
    lark = FakeLarkClient()
    notifier = make_notifier(monkeypatch, lark)
    run(notifier.send('出问题了'))

    request = lark.last('message')
    assert request.body.msg_type == 'text'
    assert loads(request.body.content) == {'text': '出问题了'}


def test_nothing_is_sent_without_target_chat(monkeypatch) -> None:
    """只填了应用凭据、没选目标群时不发送（也不报错）。"""
    lark = FakeLarkClient()
    notifier = make_notifier(monkeypatch, lark, app=False)
    run(notifier.send_account_message('主力号', {'send_user_name': '买家'}, '在吗'))

    assert lark.calls == []


def test_send_failure_raises_notify_error(monkeypatch) -> None:
    """SDK 返回失败要抛 NotifyError（Supervisor 会记事件，不会静默丢消息）。"""
    from goofishpostman.accounts import NotifyError

    lark = FakeLarkClient(message_code=230002)
    notifier = make_notifier(monkeypatch, lark)
    with raises(NotifyError, match='230002'):
        run(notifier.send('在吗'))


def test_image_is_uploaded_through_the_sdk(monkeypatch) -> None:
    """图片走 SDK 的 im.v1.image.create 换 image_key，再内嵌进卡片正文。"""
    lark = FakeLarkClient()
    downloads = FakeDownloadClient()
    notifier = make_notifier(monkeypatch, lark, downloads)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    upload = lark.last('image')
    assert upload.uri == '/open-apis/im/v1/images'
    assert upload.body.image_type == 'message'
    assert upload.body.image.name == 'message.jpg'  # multipart 里要有个像图片的文件名
    assert upload.body.image.read() == downloads.image

    elements = card_of(lark.last('message'))['body']['elements']
    # 标注行与地址行都被内嵌图片取代，正文里只剩图片本身
    assert elements[0] == {'tag': 'markdown', 'content': '![图片](img-key-1)'}
    assert IMAGE_URL not in str(elements)
    assert '[图片]' not in elements[0]['content'].splitlines()  # 没有单独的标注行


def test_image_keeps_link_when_download_not_possible(monkeypatch) -> None:
    """图片下载不到时（例如地址过期）正文里保留可点开的地址，消息照发。"""
    lark = FakeLarkClient()
    notifier = make_notifier(monkeypatch, lark, FakeDownloadClient(status_code=420))
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    assert lark.count('image') == 0
    elements = card_of(lark.last('message'))['body']['elements']
    assert elements[0]['content'] == f'[图片]\n[{IMAGE_URL}]({IMAGE_URL})'


def test_upload_failure_falls_back_to_the_link(monkeypatch) -> None:
    """上传失败不能把整条推送带崩，退回链接即可。"""
    lark = FakeLarkClient(image_code=234001)
    notifier = make_notifier(monkeypatch, lark)
    run(
        notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,))
    )

    elements = card_of(lark.last('message'))['body']['elements']
    assert elements[0]['content'] == f'[图片]\n[{IMAGE_URL}]({IMAGE_URL})'


def test_same_image_is_uploaded_once(monkeypatch) -> None:
    """同一张图（含重复下发的同一条消息）只上传/下载一次。"""
    lark = FakeLarkClient()
    downloads = FakeDownloadClient()
    notifier = make_notifier(monkeypatch, lark, downloads)
    for _ in range(3):
        run(
            notifier.send_account_message(
                '主力号', {'send_user_name': '买家'}, f'[图片]\n{IMAGE_URL}', images=(IMAGE_URL,)
            )
        )

    assert lark.count('image') == 1
    assert downloads.urls == [IMAGE_URL]


def test_list_chats_returns_groups(monkeypatch) -> None:
    """列群走 SDK 的 im.v1.chat.list（需要 im:chat:readonly 权限）。"""
    lark = FakeLarkClient()
    notifier = make_notifier(monkeypatch, lark)
    chats = run(notifier.list_chats())

    assert chats == [{'chat_id': 'oc_chat1', 'name': '卖家消息'}]
    assert ('page_size', '50') in lark.last('chat').queries


# ── 视频 / 语音 ───────────────────────────────────────────────────────────────
VIDEO_URL = 'http://example.com/clip.mp4'
COVER_URL = 'https://img.alicdn.com/cover.jpg'
OPUS_BYTES = b'OggS\x00\x02\x00\x00\x00\x00\x00\x00\x00\x00OpusHead\x01\x02' + b'\x00' * 40


def test_video_is_uploaded_and_embedded_in_the_card(monkeypatch) -> None:
    """视频：上传 mp4 换 file_key、封面换 img_key，再用 video 组件内嵌进卡片。"""
    lark = FakeLarkClient()
    downloads = FakeDownloadClient(image=b'\x00\x00\x00 ftypisom' + b'\x00' * 32)
    notifier = make_notifier(monkeypatch, lark, downloads)
    media = {'kind': 'video', 'url': VIDEO_URL, 'cover': COVER_URL, 'duration': 12}
    run(notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[视频]\n{VIDEO_URL}', media=media))

    upload = lark.last('file')
    assert upload.uri == '/open-apis/im/v1/files'
    assert upload.body.file_type == 'mp4'
    assert upload.body.file_name == 'message.mp4'
    assert upload.body.duration == 12000  # 12 秒按秒处理，换成毫秒

    card = card_of(lark.last('message'))
    # 视频组件要求关掉转发，否则卡片发不出去
    assert card['config'] == {'update_multi': True, 'enable_forward': False}
    assert card['body']['elements'][-1] == {
        'tag': 'video',
        'file_key': 'file-key-1',
        'show_time': True,
        'cover': {'img_key': 'img-key-1'},
    }
    assert VIDEO_URL not in str(card)  # 地址行与 [视频] 标注都被内嵌视频取代
    assert '[视频]' not in str(card)


def test_audio_is_sent_as_a_separate_voice_message(monkeypatch) -> None:
    """语音：OPUS 直接上传，卡片里保留 [语音] 标注，随后补发一条 audio 消息。"""
    lark = FakeLarkClient()
    notifier = make_notifier(monkeypatch, lark, FakeDownloadClient(image=OPUS_BYTES))
    media = {'kind': 'audio', 'url': 'https://example.com/voice.opus', 'cover': '', 'duration': 8}
    run(
        notifier.send_account_message(
            '主力号', {'send_user_name': '买家'}, '[语音]\nhttps://example.com/voice.opus', media=media
        )
    )

    upload = lark.last('file')
    assert upload.body.file_type == 'opus'
    assert upload.body.file_name == 'message.opus'
    assert upload.body.duration == 8000

    card_request = next(request for resource, request in lark.calls if resource == 'message')
    card = loads(card_request.body.content)
    assert card['body']['elements'][0]['content'] == '[语音]'  # 标注留着，和下面的语音对上
    assert 'example.com/voice.opus' not in str(card)  # 地址行去掉
    assert all(element['tag'] != 'video' for element in card['body']['elements'])  # 语音没有卡片组件

    audio_request = [request for resource, request in lark.calls if resource == 'message'][-1]
    assert audio_request.body.msg_type == 'audio'
    assert loads(audio_request.body.content) == {'file_key': 'file-key-1', 'duration': 8000}


def test_audio_without_opus_or_ffmpeg_falls_back_to_the_link(monkeypatch) -> None:
    """飞书只收 OPUS；本机没有 ffmpeg 时不上传，保留链接（消息照发）。"""
    monkeypatch.setattr('goofishpostman.accounts.which', lambda name: None)
    lark = FakeLarkClient()
    notifier = make_notifier(monkeypatch, lark, FakeDownloadClient(image=b'#!AMR\n\x00\x00'))
    media = {'kind': 'audio', 'url': 'https://example.com/voice.amr', 'cover': '', 'duration': 5}
    run(
        notifier.send_account_message(
            '主力号', {'send_user_name': '买家'}, '[语音]\nhttps://example.com/voice.amr', media=media
        )
    )

    assert lark.count('file') == 0
    assert lark.count('message') == 1  # 只有卡片，没有语音消息
    assert 'example.com/voice.amr' in str(card_of(lark.last('message')))


def test_audio_is_transcoded_when_ffmpeg_exists(monkeypatch) -> None:
    """本机有 ffmpeg 时把非 OPUS 语音转成 OPUS 再上传（转码本身在别的用例里不该执行）。"""
    monkeypatch.setattr('goofishpostman.accounts.which', lambda name: '/usr/bin/ffmpeg')
    monkeypatch.setattr(FeishuNotifier, '_transcode_with_ffmpeg', staticmethod(lambda data: OPUS_BYTES))
    lark = FakeLarkClient()
    notifier = make_notifier(monkeypatch, lark, FakeDownloadClient(image=b'#!AMR\n\x00\x00'))
    media = {'kind': 'audio', 'url': 'https://example.com/voice.amr', 'cover': '', 'duration': 5}
    run(
        notifier.send_account_message(
            '主力号', {'send_user_name': '买家'}, '[语音]\nhttps://example.com/voice.amr', media=media
        )
    )

    assert lark.last('file').body.file_type == 'opus'
    assert [request for resource, request in lark.calls if resource == 'message'][-1].body.msg_type == 'audio'


def test_media_upload_failure_falls_back_to_the_link(monkeypatch) -> None:
    """上传失败不能把整条推送带崩：退回链接，卡片照发。"""
    lark = FakeLarkClient(file_code=234006)
    notifier = make_notifier(monkeypatch, lark, FakeDownloadClient(image=b'\x00' * 64))
    media = {'kind': 'video', 'url': VIDEO_URL, 'cover': '', 'duration': 0}
    run(notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[视频]\n{VIDEO_URL}', media=media))

    card = card_of(lark.last('message'))
    assert card['body']['elements'][0]['content'] == f'[视频]\n[{VIDEO_URL}]({VIDEO_URL})'
    assert 'enable_forward' not in card['config']


def test_video_duration_is_read_from_the_file_when_payload_says_zero(monkeypatch) -> None:
    """报文里 duration=0（真实情况）时，从 mp4 里读时长，卡片才不会显示 00:00。"""
    from test_utils import mp4_with_duration

    lark = FakeLarkClient()
    downloads = FakeDownloadClient(image=mp4_with_duration(19412, timescale=1000))
    notifier = make_notifier(monkeypatch, lark, downloads)
    media = {'kind': 'video', 'url': VIDEO_URL, 'cover': '', 'duration': 0}
    run(notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[视频]\n{VIDEO_URL}', media=media))

    assert lark.last('file').body.duration == 19412


def test_same_video_is_uploaded_once(monkeypatch) -> None:
    """同一条消息重复下发（实测会重复 6 次）时视频只上传一次。"""
    lark = FakeLarkClient()
    downloads = FakeDownloadClient(image=b'\x00' * 64)
    notifier = make_notifier(monkeypatch, lark, downloads)
    media = {'kind': 'video', 'url': VIDEO_URL, 'cover': '', 'duration': 0}
    for _ in range(3):
        run(notifier.send_account_message('主力号', {'send_user_name': '买家'}, f'[视频]\n{VIDEO_URL}', media=media))

    assert lark.count('file') == 1
    assert downloads.urls == [VIDEO_URL]


def test_sdk_is_not_loaded_without_credentials(monkeypatch) -> None:
    """没配凭据时不该去导 SDK —— 那个导入实测要 10 秒，所以是惰性的。"""
    loaded: list[int] = []
    monkeypatch.setattr('goofishpostman.accounts.load_sdk', lambda: loaded.append(1))

    notifier = FeishuNotifier()
    run(notifier.send('hi'))

    assert loaded == []
    assert notifier.can_upload_images is False

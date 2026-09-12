"""长连接初始化 / 消息分发的回归测试（不联网）。

用 asyncio.run 直接驱动协程，避免为一个测试引入 pytest-asyncio。
"""

from __future__ import annotations

from asyncio import run
from json import dumps, loads

from loguru import logger
from pytest import raises

from goofishpostman.goofish_live import GoofishLive
from goofishpostman.types import APP_KEY

from fixtures import AROUSE_PAYLOAD, SESSION_ID, encrypted_record, plain_record, push_frame

COOKIE_STR = 'unb=123456; tracknick=tester; _m_h5_tk=T_1'


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, message: str) -> None:
        self.sent.append(loads(message))


async def _noop(message, websocket) -> None:
    return None


def make_live(token: str | None) -> GoofishLive:
    live = GoofishLive(COOKIE_STR)
    live.goofish = type('FakeGoofish', (), {'get_token': lambda self: {'data': {'accessToken': token}}})()
    return live


def test_init_sends_reg_with_channel_app_key() -> None:
    live = make_live('TOKEN')
    websocket = FakeWebSocket()
    run(live.init(websocket))

    reg, ack_diff = websocket.sent
    assert reg['lwp'] == '/reg'
    assert reg['headers']['app-key'] == APP_KEY
    assert reg['headers']['token'] == 'TOKEN'
    assert reg['headers']['did'] == live.device_id
    # 这两个是协议字段，必须与上游实现一致：
    # `sync` 写错会让服务端不往 sync 管道推真正的私信正文（只剩会话/预热通知）
    assert reg['headers']['wv'] == 'im:3,au:3,sy:6'
    assert reg['headers']['sync'] == '0,0;0;0;'
    assert ack_diff['lwp'] == '/r/SyncStatus/ackDiff'
    # pts 是微秒（timestamp * 1000）
    assert ack_diff['body'][0]['pts'] == ack_diff['body'][0]['timestamp'] * 1000


def test_init_raises_when_token_missing() -> None:
    """原实现是 exit(0)（会杀掉宿主进程），现在改为抛异常。"""
    with raises(RuntimeError, match='token'):
        run(make_live('').init(FakeWebSocket()))


def test_dispatch_message_reaches_handle_message() -> None:
    live = make_live('TOKEN')
    received = []

    async def handle_message(message, websocket) -> None:
        received.append(message)

    live.handle_message = handle_message

    payload = {
        '1': {'2': 'CID@goofish', '10': {'reminderTitle': '买家', 'reminderContent': '在吗', 'senderUserId': 'U1'}}
    }
    # 推送形态：data[0]['data']，明文 JSON 也能被 decrypt_data 直接吃下
    push = {'data': [{'data': dumps(payload, ensure_ascii=False)}]}

    run(live.dispatch_message(push, FakeWebSocket()))
    assert received[0]['cid'] == 'CID'
    assert received[0]['send_message'] == '在吗'


def test_dispatch_message_ignores_non_message_pushes() -> None:
    live = make_live('TOKEN')
    called = []

    async def handle_message(message, websocket) -> None:
        called.append(message)

    live.handle_message = handle_message
    for message in ({}, {'headers': {'mid': '1'}}, {'lwp': '/s/vulcan'}, {'body': {}}):
        run(live.dispatch_message(message, FakeWebSocket()))
    assert called == []


# ── 同步状态协商（服务端说"同步数据太长"时才会补推积压的私信）────────────────
def test_sync_extra_type_triggers_get_state() -> None:
    """回归：收到 syncExtraType(1|2) 的帧必须主动去取同步状态。

    少了这一步，服务端只会一直推会话/预热通知，永远不补推私信正文。
    """
    live = make_live('TOKEN')
    websocket = FakeWebSocket()
    frame = {'lwp': '/s/sync', 'body': {'syncExtensionModel': {'fingerprint': -1}, 'syncExtraType': {'type': 2}}}

    run(live.dispatch_message(frame, websocket))

    assert len(websocket.sent) == 1
    request = websocket.sent[0]
    assert request['lwp'] == '/r/SyncStatus/getState'
    assert request['body'] == [{'topic': 'sync'}]
    assert live._pending_sync_mid == request['headers']['mid']


def test_sync_state_response_is_acknowledged() -> None:
    """拿到同步状态后要回执 /r/SyncStatus/ackDiff，服务端才会补推记录。"""
    live = make_live('TOKEN')
    websocket = FakeWebSocket()
    marker = {'lwp': '/s/sync', 'body': {'syncExtraType': {'type': 1}}}
    run(live.dispatch_message(marker, websocket))

    state = {'pipeline': 'sync', 'pts': 1, 'seq': 0}
    response = {'code': 200, 'headers': {'mid': live._pending_sync_mid}, 'body': state}
    run(live.dispatch_message(response, websocket))

    assert [item['lwp'] for item in websocket.sent] == ['/r/SyncStatus/getState', '/r/SyncStatus/ackDiff']
    assert websocket.sent[1]['body'] == [state]
    assert live._pending_sync_mid is None


def test_sync_marker_frame_is_not_treated_as_message() -> None:
    """协商帧本身不带业务记录，不能当成私信。"""
    live = make_live('TOKEN')
    called = []

    async def handle_message(message, websocket) -> None:
        called.append(message)

    live.handle_message = handle_message
    frame = {'body': {'syncExtraType': {'type': 2}}}
    run(live.dispatch_message(frame, FakeWebSocket()))
    assert called == []


# ── 会话/预热记录：从里面学「会话 → 商品标题」（卡片上的商品靠它）──────────────
def test_session_record_teaches_the_item_title() -> None:
    live = make_live('TOKEN')
    learned: list[tuple[str, str]] = []
    live.on_session_info = lambda sid, title: learned.append((sid, title))

    run(live.dispatch_message(push_frame(plain_record(AROUSE_PAYLOAD)), FakeWebSocket()))

    assert learned == [(SESSION_ID, '上海gan部在线学习笔记，详情请咨询。标价2026年全年包年')]


def test_message_frame_does_not_teach_a_title() -> None:
    """私信帧里没有商品标题，别往上回调垃圾。"""
    live = make_live('TOKEN')
    learned: list[tuple[str, str]] = []
    live.on_session_info = lambda sid, title: learned.append((sid, title))
    live.handle_message = _noop

    run(live.dispatch_message(push_frame(encrypted_record()), FakeWebSocket()))

    assert learned == []


def test_session_info_hook_is_optional() -> None:
    """没有上层回调时也不能炸（命令行单独跑时就是这样）。"""
    live = make_live('TOKEN')
    run(live.dispatch_message(push_frame(plain_record(AROUSE_PAYLOAD)), FakeWebSocket()))


# ── 解析失败告警：要打记录本身，而不是每条都一样的帧头 ────────────────────────
def test_failed_parse_warning_describes_records() -> None:
    """报文形状变了时要能在日志里看到记录内容。

    原来的实现打整帧前 300 字符 —— 帧开头永远是 headers，每条告警长得一模一样，
    实际排查时根本看不出是哪条消息出的问题。
    """
    live = make_live('TOKEN')
    broken = encrypted_record({'9': '没有消息字段的负载'})
    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(str(message)), level='WARNING')
    try:
        run(live.dispatch_message(push_frame(broken), FakeWebSocket()))
    finally:
        logger.remove(sink_id)

    warning = next(text for text in messages if '私信记录解析失败' in text)
    assert 'objectType=40' in warning
    assert '没有消息字段的负载' in warning
    assert 'headers' not in warning

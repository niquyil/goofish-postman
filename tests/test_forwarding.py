"""端到端回放：把真实抓到的推送帧喂给 Supervisor，验证转发与去重。

覆盖用户报的问题：闲鱼私信没有被转发到飞书、网页也没有日志。
"""

from __future__ import annotations

from asyncio import Event, run

from goofishpostman.sender import pick_header_color
from goofishpostman.store import Store
from goofishpostman.supervisor import Supervisor

from fixtures import (
    AROUSE_PAYLOAD,
    IMAGE_CONTENT,
    IMAGE_URL,
    PUSH_MESSAGE_PAYLOAD,
    SESSION_ID,
    TRADE_CARD_CONTENT,
    encrypted_record,
    plain_record,
    push_frame,
    push_payload_with_content,
)
from helpers import RECEIVER_COOKIE, RecordingNotifier

# cookie 里的 unb/昵称要与真实报文一致，否则归属判断与展示名会对不上
ACCOUNT_LABEL = '网课学习私人助理(主力号)'


class IdleLive:
    """不发任何消息的假连接：回放测试要精确控制"收到哪些帧"。"""

    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self.handle_message = None

    async def main(self) -> None:
        await Event().wait()


def make_supervisor(tmp_dir) -> tuple[Supervisor, RecordingNotifier]:
    store = Store(tmp_dir / 'accounts.json')
    store.add(name='主力号', cookie=RECEIVER_COOKIE, enabled=True)
    notifier = RecordingNotifier()
    supervisor = Supervisor(store, notifier)
    supervisor.live_factory = IdleLive
    return supervisor, notifier


def replay(supervisor: Supervisor, frame: dict) -> None:
    """把帧交给账号的 handler，等价于长连接收到推送。"""

    async def run_scenario() -> None:
        await supervisor.start_enabled()
        account = supervisor.store.list_accounts()[0]
        runtime = supervisor.runtimes[account.id]
        handler = supervisor._make_handler(account, runtime, lambda: None)
        await handler(_parse(frame), None)

    run(run_scenario())


def _parse(frame: dict):
    """用真实的解析入口把帧转成 MessageInfo（等价于 dispatch_message 内部）。"""
    from goofishpostman.goofish_utils import extract_message_info

    return extract_message_info(frame)


def test_real_message_is_forwarded(tmp_dir, stub_decrypt) -> None:
    supervisor, notifier = make_supervisor(tmp_dir)
    replay(supervisor, push_frame(encrypted_record()))
    assert len(notifier.sent) == 1
    # 文案：发送方昵称 → 接收方「昵称(账号)」，不带【】前缀
    assert notifier.sent[0] == f'一站式学习助手 → {ACCOUNT_LABEL}\n有吗'
    assert '【' not in notifier.sent[0]


def test_duplicate_frames_are_forwarded_once(tmp_dir, stub_decrypt) -> None:
    """同一条私信会随多个帧重复下发（实测抓到 6 次），只能推一次。"""
    supervisor, notifier = make_supervisor(tmp_dir)
    for _ in range(6):
        replay(supervisor, push_frame(encrypted_record()))
    assert len(notifier.sent) == 1
    # 网页消息流也只应有一条
    assert len(supervisor.messages) == 1


def test_arouse_records_are_never_forwarded(tmp_dir, stub_decrypt) -> None:
    """会话预热数据不是私信，不能推给飞书。"""
    supervisor, notifier = make_supervisor(tmp_dir)
    supervisor.publish_event('info', '', 'sentinel')
    before = len(supervisor.messages)
    # dispatch_message 会捕获 KeyError 并记 debug 日志，不应该产生消息记录
    from goofishpostman.goofish_live import GoofishLive

    live = GoofishLive(RECEIVER_COOKIE)

    async def run_scenario() -> None:
        await live.dispatch_message(push_frame(plain_record(AROUSE_PAYLOAD)), None)

    run(run_scenario())
    assert notifier.sent == []
    assert len(supervisor.messages) == before


def test_mixed_batch_forwards_only_the_message(tmp_dir, stub_decrypt) -> None:
    """真实批次里预热+私信混在一起，只转发私信。"""
    supervisor, notifier = make_supervisor(tmp_dir)
    replay(supervisor, push_frame(plain_record(AROUSE_PAYLOAD), encrypted_record(), plain_record(AROUSE_PAYLOAD)))
    assert len(notifier.sent) == 1
    assert '有吗' in notifier.sent[0]


def test_forwarded_card_carries_message_details(tmp_dir, stub_decrypt) -> None:
    """飞书卡片的明细只要时间和商品名（其余字段别塞进去，免得抢正文的注意力）。"""
    supervisor, notifier = make_supervisor(tmp_dir)
    replay(supervisor, push_frame(encrypted_record()))

    card = notifier.card_payloads[0]
    assert card['title'] == f'一站式学习助手 → {ACCOUNT_LABEL}'
    assert card['content'] == '有吗'
    assert list(card['details']) == ['时间']  # 还没学到商品标题
    # 账号配色固定，同一个账号每次都是同一个颜色
    assert card['color'] == pick_header_color(supervisor.store.list_accounts()[0].id)


def test_learned_item_title_appears_on_the_card(tmp_dir, stub_decrypt) -> None:
    """会话预热记录学到商品标题后，该会话的私信卡片要带上「商品」（时间在前）。"""
    supervisor, notifier = make_supervisor(tmp_dir)
    supervisor._remember_session_title(SESSION_ID, '上海迪士尼玲娜贝儿钱包')
    replay(supervisor, push_frame(encrypted_record()))
    details = notifier.card_payloads[0]['details']
    assert list(details) == ['时间', '商品']
    assert details['商品'] == '上海迪士尼玲娜贝儿钱包'


def test_long_item_title_is_truncated(tmp_dir, stub_decrypt) -> None:
    """商品标题很长时截断，别让明细行挤爆卡片。"""
    supervisor, notifier = make_supervisor(tmp_dir)
    supervisor._remember_session_title(SESSION_ID, '长' * 50)
    replay(supervisor, push_frame(encrypted_record()))
    assert notifier.card_payloads[0]['details']['商品'] == f'{"长" * 30}…'


def test_session_title_cache_is_bounded(tmp_dir) -> None:
    """会话表不能无限增长（每个会话一条商品标题）。"""
    from goofishpostman.supervisor import _MAX_SESSION_TITLES

    supervisor, _ = make_supervisor(tmp_dir)
    for index in range(_MAX_SESSION_TITLES + 20):
        supervisor._remember_session_title(f'sid-{index}', f'标题{index}')
    assert len(supervisor._session_titles) == _MAX_SESSION_TITLES


def test_image_message_is_forwarded_with_its_url(tmp_dir, stub_decrypt) -> None:
    """非文本消息也要转发，并且把图片地址带出去（网页消息流与卡片正文同一份文案）。"""
    supervisor, notifier = make_supervisor(tmp_dir)
    replay(supervisor, push_frame(encrypted_record(push_payload_with_content(IMAGE_CONTENT, '[图片]'))))

    card = notifier.card_payloads[0]
    assert card['content'] == f'[图片]\n{IMAGE_URL}'
    assert supervisor.messages[-1].text == f'[图片]\n{IMAGE_URL}'


def test_trade_card_message_is_forwarded_with_its_text(tmp_dir, stub_decrypt) -> None:
    """交易卡片要给标题与说明，而不是只转发报文里那句 `[提醒开通微信收款]`。"""
    supervisor, notifier = make_supervisor(tmp_dir)
    replay(
        supervisor, push_frame(encrypted_record(push_payload_with_content(TRADE_CARD_CONTENT, '[提醒开通微信收款]')))
    )

    text = notifier.card_payloads[0]['content']
    assert text.startswith('[交易卡片]\n我已修改价格，等待你付款')
    assert '去付款：fleamarket://order_detail?id=1&role=buyer' in text


def test_extract_message_uid_extracted_from_real_payload() -> None:
    """去重依赖的 messageId 必须能从真实报文里取到。"""
    from goofishpostman.goofish_utils import extract_message_uid

    assert extract_message_uid(PUSH_MESSAGE_PAYLOAD) == 'b419afeaa64c4e11aab5fc5a8b12556c'


def test_dedup_cache_is_bounded(tmp_dir) -> None:
    """去重表按账号分别保留，且不会无限增长。"""
    from goofishpostman.supervisor import _MAX_SEEN_MESSAGES

    supervisor, _ = make_supervisor(tmp_dir)
    for index in range(_MAX_SEEN_MESSAGES + 50):
        assert supervisor._is_duplicate('acct', f'id-{index}') is False
    assert len(supervisor._seen_messages['acct']) == _MAX_SEEN_MESSAGES
    # 最早的那些已经被挤出，不再算重复
    assert supervisor._is_duplicate('acct', 'id-0') is False

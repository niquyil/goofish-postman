"""消息归属与「自己发的不推送」的测试。

真实规律（两台账号同时抓包 + 用户实测确认）：
- 账号 A 发言时，A 与 B 的连接都会收到这同一条消息
- 报文里 senderUserId 是发送方闲鱼账号 id，昵称在提醒标题里
- 受监控账号自己的 unb 与昵称都在它自己的 cookie 里（unb / tracknick）

因此：
1. 发送方就是当前监听账号 → 不推送（否则 A 会把自己发的话当成"收到消息"）
2. 接收方那一侧正常推送，接收方就是该监听账号自己
"""

from __future__ import annotations

from asyncio import Event, run, sleep

from goofishpostman.store import Store
from goofishpostman.supervisor import Supervisor
from goofishpostman.types import MessageInfo

from helpers import RecordingNotifier

KISUKE_UNB = '13993122'
KISUKE_NICK = '网课学习私人助理'
OTHER_UNB = '2221114099805'
OTHER_NICK = 'xy773249480508'

KISUKE_COOKIE = f'unb={KISUKE_UNB}; tracknick={KISUKE_NICK}; _m_h5_tk=tk_1'
OTHER_COOKIE = f'unb={OTHER_UNB}; tracknick={OTHER_NICK}; _m_h5_tk=tk_2'


class IdleLive:
    def __init__(self, cookie: str) -> None:
        self.cookie = cookie
        self.handle_message = None

    async def main(self) -> None:
        await Event().wait()


class Harness:
    def __init__(self, supervisor: Supervisor, notifier: RecordingNotifier) -> None:
        self.supervisor = supervisor
        self.notifier = notifier
        self.kisuke, self.other = (a.id for a in supervisor.store.list_accounts())

    def deliver(self, account_id: str, message: MessageInfo) -> None:
        async def run_scenario() -> None:
            record = self.supervisor.store.get(account_id)
            runtime = self.supervisor.runtimes[account_id]
            handler = self.supervisor._make_handler(account=record, runtime=runtime, on_message=lambda: None)
            await handler(message, None)

        run(run_scenario())


def make_harness(tmp_dir) -> Harness:
    store = Store(tmp_dir / 'accounts.json')
    store.add(name='kisuke', cookie=KISUKE_COOKIE, enabled=True)
    store.add(name=OTHER_NICK, cookie=OTHER_COOKIE, enabled=True)
    notifier = RecordingNotifier()
    supervisor = Supervisor(store=store, notifier=notifier)
    supervisor.live_factory = IdleLive
    supervisor.sync_runtimes()
    return Harness(supervisor=supervisor, notifier=notifier)


def make_message(sender_id: str, sender_name: str, text: str, uid: str) -> MessageInfo:
    return {
        'cid': '54995284239',
        'send_user_id': sender_id,
        'send_user_name': sender_name,
        'send_message': text,
        'raw': {'1': {'1': {'10': {'bizTag': f'{{"messageId":"{uid}"}}'}}}},
    }


# ── 身份字段 ──────────────────────────────────────────────────────────────────
def test_account_exposes_unb_and_nickname(tmp_dir) -> None:
    harness = make_harness(tmp_dir)
    first, second = harness.supervisor.store.list_accounts()
    assert first.unb == KISUKE_UNB
    assert first.nickname == KISUKE_NICK
    assert second.nickname == OTHER_NICK
    assert first.aliases == {'kisuke', KISUKE_NICK}


def test_build_account_map_maps_nickname_to_account_id(tmp_dir) -> None:
    """昵称 → 账号 id 对照表（用报文里的昵称反查账号）。"""
    harness = make_harness(tmp_dir)
    mapping = harness.supervisor.build_account_map()
    assert mapping[KISUKE_NICK] == KISUKE_UNB
    assert mapping[OTHER_NICK] == OTHER_UNB
    assert mapping['kisuke'] == KISUKE_UNB  # 备注名也可识别


def test_sender_name_falls_back_to_build_account_map(tmp_dir) -> None:
    """报文没带发送方昵称时，用对照表按账号 id 反查昵称。"""
    harness = make_harness(tmp_dir)
    supervisor = harness.supervisor

    assert supervisor._resolve_sender_name(KISUKE_UNB) == KISUKE_NICK
    assert supervisor._resolve_sender_name('99999999') == '99999999'  # 陌生账号退回 id
    assert supervisor._resolve_sender_name('') == ''


def test_message_without_sender_name_uses_mapping(tmp_dir) -> None:
    """报文没带发送方昵称时，用对照表按 id 反查出昵称。"""
    harness = make_harness(tmp_dir)
    harness.deliver(
        account_id=harness.kisuke, message=make_message(sender_id=OTHER_UNB, sender_name='', text='你好', uid='m-map')
    )

    assert [m.send_user_name for m in harness.supervisor.messages] == [OTHER_NICK]
    assert OTHER_NICK in harness.notifier.sent[0]


# ── 自己发的不推送 ────────────────────────────────────────────────────────────
def test_own_message_is_not_forwarded(tmp_dir) -> None:
    """kisuke 自己发的消息，在 kisuke 这一侧不应推送。"""
    harness = make_harness(tmp_dir)
    message = make_message(sender_id=KISUKE_UNB, sender_name=KISUKE_NICK, text='我发的', uid='m1')

    harness.deliver(account_id=harness.kisuke, message=message)

    assert harness.notifier.sent == []
    assert harness.supervisor.messages == []


def test_own_message_reaches_the_other_account(tmp_dir) -> None:
    """同一条消息在接收方那一侧要正常推送。"""
    harness = make_harness(tmp_dir)
    message = make_message(sender_id=KISUKE_UNB, sender_name=KISUKE_NICK, text='大号发的消息', uid='m2')

    harness.deliver(account_id=harness.kisuke, message=message)  # 发送方自己 → 不推
    harness.deliver(account_id=harness.other, message=message)  # 接收方 → 推

    assert len(harness.notifier.sent) == 1
    assert OTHER_NICK in harness.notifier.sent[0]
    assert [m.account_name for m in harness.supervisor.messages] == [OTHER_NICK]


def test_buyer_message_is_forwarded_by_the_receiving_account(tmp_dir) -> None:
    """买家消息的发送方不是任何被监控账号，两侧都不该被当成"自己发的"。"""
    harness = make_harness(tmp_dir)
    message = make_message(sender_id='99999999', sender_name='买家小王', text='在吗', uid='m3')

    harness.deliver(account_id=harness.other, message=message)

    assert len(harness.notifier.sent) == 1
    assert '买家小王' in harness.notifier.sent[0]


def test_is_self_sent_prefers_account_id(tmp_dir) -> None:
    harness = make_harness(tmp_dir)
    supervisor = harness.supervisor
    kisuke, other = supervisor.store.list_accounts()

    assert supervisor.is_self_sent(account=kisuke, sender_id=KISUKE_UNB, sender_name=KISUKE_NICK) is True
    assert supervisor.is_self_sent(account=kisuke, sender_id=OTHER_UNB, sender_name=KISUKE_NICK) is False
    # 没有 id 时退回昵称判断
    assert supervisor.is_self_sent(account=kisuke, sender_id='', sender_name=KISUKE_NICK) is True
    assert supervisor.is_self_sent(account=kisuke, sender_id='', sender_name='陌生人') is False
    assert supervisor.is_self_sent(account=other, sender_id='', sender_name=OTHER_NICK) is True


def test_message_without_sender_id_uses_nickname(tmp_dir) -> None:
    """报文缺 senderUserId 时，靠昵称仍能识别出是自己发的。"""
    harness = make_harness(tmp_dir)
    harness.deliver(
        account_id=harness.kisuke, message=make_message(sender_id='', sender_name=KISUKE_NICK, text='我发的', uid='m4')
    )
    assert harness.notifier.sent == []


# ── 去重（按账号） ────────────────────────────────────────────────────────────
def test_dedup_is_per_account(tmp_dir) -> None:
    """同一账号收到重复帧只推一次；另一个账号不受其影响。"""
    harness = make_harness(tmp_dir)
    message = make_message(sender_id='99999999', sender_name='买家', text='买家消息', uid='m5')

    for _ in range(5):
        harness.deliver(account_id=harness.kisuke, message=message)
    assert len(harness.notifier.sent) == 1

    harness.deliver(account_id=harness.other, message=message)
    assert len(harness.notifier.sent) == 2


def test_duplicate_without_id_is_not_deduped(tmp_dir) -> None:
    """没有 messageId 时不做去重（宁可重复也不漏）。"""
    harness = make_harness(tmp_dir)
    message = make_message(sender_id='99999999', sender_name='买家', text='无 id', uid='')
    message['raw'] = {}
    harness.deliver(account_id=harness.kisuke, message=message)
    harness.deliver(account_id=harness.kisuke, message=message)
    assert len(harness.notifier.sent) == 2


def test_dedup_cache_is_bounded_per_account(tmp_dir) -> None:
    from goofishpostman.supervisor import _MAX_SEEN_MESSAGES

    supervisor = make_harness(tmp_dir).supervisor
    for index in range(_MAX_SEEN_MESSAGES + 50):
        assert supervisor._is_duplicate(account_id='acct', uid=f'id-{index}') is False
    assert len(supervisor._seen_messages['acct']) == _MAX_SEEN_MESSAGES
    assert supervisor._is_duplicate(account_id='acct', uid='id-0') is False


# ── 昵称：cookie 的 tracknick 可能是旧值，要从报文里学 ────────────────────────
# 真实抓包：某账号 cookie 里 tracknick 是英文旧名，报文里的昵称才是中文真名
STALE_COOKIE = f'unb={KISUKE_UNB}; tracknick=kisuke_old; _m_h5_tk=tk_1'
STALE_NICK = 'kisuke_old'
REAL_NICK = '网课学习私人助理'


def make_stale_harness(tmp_dir) -> Harness:
    """kisuke 的 cookie 昵称是过期的英文名，用来验证从报文学习。"""
    store = Store(tmp_dir / 'accounts.json')
    store.add(name='kisuke', cookie=STALE_COOKIE, enabled=True)
    store.add(name=OTHER_NICK, cookie=OTHER_COOKIE, enabled=True)
    notifier = RecordingNotifier()
    supervisor = Supervisor(store=store, notifier=notifier)
    supervisor.live_factory = IdleLive
    supervisor.sync_runtimes()
    return Harness(supervisor=supervisor, notifier=notifier)


def test_real_nickname_is_learned_from_payload(tmp_dir) -> None:
    """cookie 里是英文旧名，报文里是中文真名，应学到并持久化。"""
    harness = make_stale_harness(tmp_dir)
    assert harness.supervisor.store.list_accounts()[0].nickname == STALE_NICK

    # kisuke 发言：报文给出真实中文昵称
    harness.deliver(
        account_id=harness.other,
        message=make_message(sender_id=KISUKE_UNB, sender_name=REAL_NICK, text='在吗', uid='nick-1'),
    )

    updated = next(a for a in harness.supervisor.store.list_accounts() if a.unb == KISUKE_UNB)
    assert updated.nickname == REAL_NICK
    assert updated.label == f'{REAL_NICK}(kisuke)'
    # 已落盘
    assert any(a.nickname == REAL_NICK for a in Store(harness.supervisor.store.path).list_accounts())


def test_learned_nickname_is_used_for_receiver_display(tmp_dir) -> None:
    """学到之后，该账号出现在接收方位置也要显示真实昵称。"""
    harness = make_stale_harness(tmp_dir)
    # 先让 kisuke 的昵称被学到
    harness.deliver(
        account_id=harness.other,
        message=make_message(sender_id=KISUKE_UNB, sender_name=REAL_NICK, text='在吗', uid='nick-2'),
    )
    # 再由 OTHER 发消息给 kisuke，此时接收方是 kisuke，应显示中文昵称
    harness.deliver(
        account_id=harness.kisuke,
        message=make_message(sender_id=OTHER_UNB, sender_name='小号', text='收到', uid='nick-3'),
    )

    receiver = harness.supervisor.messages[-1].receiver
    assert receiver == f'{REAL_NICK}(kisuke)'
    assert REAL_NICK in harness.notifier.sent[-1]


def test_nickname_learned_once(tmp_dir) -> None:
    """同一个昵称只记一条事件，避免两个连接各学一次刷屏。"""
    harness = make_stale_harness(tmp_dir)
    for index in range(3):
        harness.deliver(
            account_id=harness.other,
            message=make_message(sender_id=KISUKE_UNB, sender_name=REAL_NICK, text='hi', uid=f'n-{index}'),
        )

    learned_events = [e for e in harness.supervisor.events if '昵称更新' in e.message]
    assert len(learned_events) == 1


def test_nickname_not_overwritten_by_numeric_id(tmp_dir) -> None:
    """报文昵称等于 id 时（没给出真名）不覆盖已有昵称。"""
    harness = make_stale_harness(tmp_dir)
    harness.deliver(
        account_id=harness.other,
        message=make_message(sender_id=KISUKE_UNB, sender_name=KISUKE_UNB, text='hi', uid='n-num'),
    )
    assert next(a for a in harness.supervisor.store.list_accounts() if a.unb == KISUKE_UNB).nickname == STALE_NICK


def test_nickname_falls_back_to_cookie_value(tmp_dir) -> None:
    """展示为「昵称(备注名)」；备注名让账号一眼可辨（不是数字 id）。"""
    harness = make_harness(tmp_dir)
    assert harness.supervisor.store.list_accounts()[0].label == f'{KISUKE_NICK}(kisuke)'


def test_label_uses_remark_name_not_numeric_id(tmp_dir) -> None:
    """括号里放备注名，不放数字账号；两者相同时不重复显示。"""
    from goofishpostman.store import Account

    learned = Account(name='kisuke', cookie=f'unb={KISUKE_UNB}; tracknick=kisuke', nickname_override='网课学习私人助理')
    assert learned.label == '网课学习私人助理(kisuke)'
    assert KISUKE_UNB not in learned.label

    # cookie 里的旧值恰好等于备注名时不显示 kisuke(kisuke)
    stale = Account(name='kisuke', cookie=f'unb={KISUKE_UNB}; tracknick=kisuke')
    assert stale.label == 'kisuke'

    # 没有备注名时退回昵称
    unnamed = Account(cookie=f'unb={KISUKE_UNB}; tracknick=网课学习私人助理')
    assert unnamed.label == '网课学习私人助理'


# ── 昵称缓存（本地持久化 + 变化检测） ─────────────────────────────────────────
def test_nickname_cache_survives_restart(tmp_dir) -> None:
    """学到的昵称缓存在本地，重启后直接复用，不必再等对方发消息。"""
    harness = make_stale_harness(tmp_dir)
    harness.deliver(
        account_id=harness.other,
        message=make_message(sender_id=KISUKE_UNB, sender_name=REAL_NICK, text='hi', uid='cache-1'),
    )

    reloaded = Store(harness.supervisor.store.path)
    cached = next(a for a in reloaded.list_accounts() if a.unb == KISUKE_UNB)
    assert cached.nickname_override == REAL_NICK
    assert cached.nickname == REAL_NICK
    assert cached.label == f'{REAL_NICK}(kisuke)'


def test_nickname_change_is_detected_and_cached(tmp_dir) -> None:
    """昵称变了要更新缓存（每次消息都检查）。"""
    harness = make_stale_harness(tmp_dir)
    harness.deliver(
        account_id=harness.other,
        message=make_message(sender_id=KISUKE_UNB, sender_name=REAL_NICK, text='hi', uid='chg-1'),
    )
    harness.deliver(
        account_id=harness.other,
        message=make_message(sender_id=KISUKE_UNB, sender_name='改名后的昵称', text='hi', uid='chg-2'),
    )

    updated = next(a for a in Store(harness.supervisor.store.path).list_accounts() if a.unb == KISUKE_UNB)
    assert updated.nickname == '改名后的昵称'


def test_nickname_unchanged_does_not_rewrite(tmp_dir) -> None:
    """昵称没变就不落盘、不记事件。"""
    harness = make_stale_harness(tmp_dir)
    harness.deliver(
        account_id=harness.other,
        message=make_message(sender_id=KISUKE_UNB, sender_name=REAL_NICK, text='hi', uid='same-1'),
    )
    updated_at_first = next(a for a in harness.supervisor.store.list_accounts() if a.unb == KISUKE_UNB).updated_at
    events_after_first = len([e for e in harness.supervisor.events if '昵称更新' in e.message])

    for index in range(3):
        harness.deliver(
            account_id=harness.other,
            message=make_message(sender_id=KISUKE_UNB, sender_name=REAL_NICK, text='hi', uid=f'same-{index + 2}'),
        )

    account = next(a for a in harness.supervisor.store.list_accounts() if a.unb == KISUKE_UNB)
    assert account.updated_at == updated_at_first
    assert len([e for e in harness.supervisor.events if '昵称更新' in e.message]) == events_after_first


def test_reset_nickname_clears_cache(tmp_dir) -> None:
    """重置昵称后回到 cookie 值，下次消息再重新学。"""
    harness = make_stale_harness(tmp_dir)
    store = harness.supervisor.store
    harness.deliver(
        account_id=harness.other,
        message=make_message(sender_id=KISUKE_UNB, sender_name=REAL_NICK, text='hi', uid='rst-1'),
    )
    assert next(a for a in store.list_accounts() if a.unb == KISUKE_UNB).nickname == REAL_NICK

    cleared = store.clear_nickname(harness.kisuke)
    assert cleared is not None and cleared.nickname_override == ''
    assert cleared.nickname == STALE_NICK  # 回到 cookie 里的旧值

    # 再收到该账号的消息会重新学到
    harness.deliver(
        account_id=harness.other,
        message=make_message(sender_id=KISUKE_UNB, sender_name=REAL_NICK, text='hi', uid='rst-2'),
    )
    assert next(a for a in store.list_accounts() if a.unb == KISUKE_UNB).nickname == REAL_NICK


def test_cookie_nickname_staleness_detection(tmp_dir) -> None:
    """cookie 昵称等于账号 id 或备注名时视为不可信（这些正是登录时写回的旧值）。"""
    from goofishpostman.store import Account

    assert Account(name='kisuke', cookie='unb=13993122; tracknick=kisuke').cookie_nickname_looks_stale is True
    assert Account(name='kisuke', cookie='unb=13993122; tracknick=13993122').cookie_nickname_looks_stale is True
    assert (
        Account(name='kisuke', cookie='unb=13993122; tracknick=网课学习私人助理').cookie_nickname_looks_stale is False
    )
    assert Account(name='kisuke', cookie='unb=13993122').cookie_nickname_looks_stale is True


# ── 账号上线事件 ──────────────────────────────────────────────────────────────
def test_connected_event_is_published_and_broadcast(tmp_dir) -> None:
    """账号连上就写事件日志并广播（网页事件日志要立刻有反馈，不该等新消息）。"""
    from helpers import FakeLive

    supervisor = make_harness(tmp_dir).supervisor
    supervisor.live_factory = FakeLive  # FakeLive 连上后交付一条消息，触发 on_connected

    received: list[dict] = []
    unsubscribe = supervisor.subscribe(received.append)

    async def run_scenario() -> None:
        await supervisor.start_enabled()
        for _ in range(200):
            if any('已连接' in e.message for e in supervisor.events):
                break
            await sleep(0.01)
        await supervisor.stop_all()

    run(run_scenario())
    unsubscribe()

    connected = [e for e in supervisor.events if '已连接' in e.message]
    # harness 里有两个账号，各自连上都会记一条
    assert len(connected) == 2
    assert any('kisuke' in e.message for e in connected)
    broadcast = [e for e in received if e.get('type') == 'event' and '已连接' in e['event']['message']]
    assert len(broadcast) == 2

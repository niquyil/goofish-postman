"""多账号调度（Supervisor）的测试：全部用假的长连接，不联网。"""

from __future__ import annotations

from asyncio import get_running_loop, run, sleep
from datetime import UTC, datetime, timedelta

from pytest import mark

from goofishpostman.goofish_utils import carries_message
from goofishpostman.store import Store
from goofishpostman.supervisor import MessageRecord, Supervisor, compute_next_backoff, format_time

from fixtures import AROUSE_PAYLOAD, encrypted_record, plain_record, push_frame
from helpers import (
    GOOD_COOKIE,
    ConnectOnlyLive,
    ExpiredCookieLive,
    FailingLive,
    FakeLive,
    RecordingLiveFactory,
    RecordingNotifier,
)


def make_supervisor(tmp_dir, *, enabled: bool = True) -> tuple[Supervisor, Store, RecordingNotifier]:
    store = Store(tmp_dir / 'a.json')
    store.add(name='主力号', cookie=GOOD_COOKIE, enabled=enabled)
    notifier = RecordingNotifier()
    supervisor = Supervisor(store=store, notifier=notifier)
    supervisor.live_factory = FakeLive
    return supervisor, store, notifier


async def wait_for(predicate, timeout: float = 2.0) -> None:
    """轮询等待条件成立，避免测试用固定 sleep 造成不稳定。"""
    deadline = get_running_loop().time() + timeout
    while get_running_loop().time() < deadline:
        if predicate():
            return
        await sleep(0.01)
    raise AssertionError('等待超时')


def test_events_without_account_omit_the_bracket_prefix(tmp_dir) -> None:
    """与账号无关的事件（飞书配置、全局的回复转发）不该在日志里留一个空的 [] 前缀。"""
    from loguru import logger

    supervisor, _, _ = make_supervisor(tmp_dir)
    lines: list[str] = []
    sink = logger.add(lambda message: lines.append(str(message)), level='INFO')
    try:
        supervisor.publish_event(level='info', account_id='', message='飞书推送配置已更新')
        supervisor.publish_event(level='info', account_id='acct-1', message='主力号 已连接')
    finally:
        logger.remove(sink)

    global_line = next(line for line in lines if line.rstrip().endswith('飞书推送配置已更新'))
    assert '[' not in global_line
    assert any('[acct-1] 主力号 已连接' in line for line in lines)
    # 事件流里仍然带着 account_id（空字符串表示全局），界面按它区分
    assert [event.account_id for event in supervisor.events] == ['', 'acct-1']


# ── 退避策略 ──────────────────────────────────────────────────────────────────
def test_backoff_doubles_while_failing_fast() -> None:
    assert compute_next_backoff(2, alive_for=1, healthy=False) == 4
    assert compute_next_backoff(4, alive_for=1, healthy=False) == 8
    assert compute_next_backoff(40, alive_for=1, healthy=False) == 60  # 封顶


def test_backoff_resets_when_connection_was_healthy() -> None:
    assert compute_next_backoff(40, alive_for=1, healthy=True) == 2.0
    assert compute_next_backoff(40, alive_for=120, healthy=False) == 2.0


# ── 时间展示 ──────────────────────────────────────────────────────────────────
def test_format_time_shows_local_time_for_utc_values() -> None:
    """时间统一按 UTC 存，界面上要换成本机时区，否则会差几个钟头。"""
    moment = datetime(year=2026, month=9, day=12, hour=4, minute=30, tzinfo=UTC)
    assert format_time(moment) == moment.astimezone().strftime('%m-%d %H:%M:%S')


def test_format_time_keeps_legacy_naive_values() -> None:
    """老配置文件里是不带时区的本地时间：原样显示，不做二次换算。"""
    assert format_time('2026-09-12T15:30:00') == '09-12 15:30:00'
    assert format_time('2026-09-12T15:30:00+00:00') == format_time(
        datetime(year=2026, month=9, day=12, hour=15, minute=30, tzinfo=UTC)
    )
    assert format_time(None) == ''
    assert format_time('不是时间') == '不是时间'


def test_message_record_timestamp_is_timezone_aware() -> None:
    """记录的时间戳要带时区：不带的话和带时区的值比较会直接 TypeError。"""
    record = MessageRecord(account_id='a', account_name='号', send_user_name='买家', text='在吗')
    assert record.at.tzinfo is not None
    public = record.to_public()
    assert public['at'].endswith(('+00:00', 'Z'))
    assert public['at_text'] == format_time(record.at)


# ── 启动 / 停止 ───────────────────────────────────────────────────────────────
async def _run_scenario(tmp_dir, body) -> None:
    await body()


def test_start_enabled_starts_accounts(tmp_dir) -> None:
    async def run_scenario() -> None:
        FakeLive.instances.clear()
        supervisor, store, notifier = make_supervisor(tmp_dir)
        await supervisor.start_enabled()
        account_id = store.list_accounts()[0].id
        await wait_for(lambda: supervisor.runtimes[account_id].status == 'running')
        await wait_for(lambda: len(notifier.sent) == 1)

        runtime = supervisor.runtimes[account_id]
        assert runtime.message_count == 1
        assert runtime.last_message_at is not None
        assert notifier.sent[0] == '买家 → tester(主力号)\n在吗'
        assert [m.text for m in supervisor.messages] == ['在吗']
        assert [m.account_name for m in supervisor.messages] == ['主力号']

        await supervisor.stop_all()
        assert runtime.status == 'stopped'
        assert runtime.task is None

    run(run_scenario())


def test_connected_account_is_running_without_any_message(tmp_dir) -> None:
    """回归：账号状态与「已连接」事件以前只在收到第一条消息时才产生。

    结果是不发消息的账号在网页上一直显示"启动中"、事件列表空着，
    看起来就像没连上 —— 现在握手成功即置为监听中。
    """

    async def run_scenario() -> None:
        supervisor, store, notifier = make_supervisor(tmp_dir)
        supervisor.live_factory = ConnectOnlyLive
        await supervisor.start_enabled()
        account_id = store.list_accounts()[0].id
        await wait_for(lambda: supervisor.runtimes[account_id].status == 'running')

        runtime = supervisor.runtimes[account_id]
        assert runtime.started_at is not None
        assert runtime.message_count == 0  # 一条消息都没收到
        assert notifier.sent == []  # 也不该有推送
        assert any('已连接' in event.message for event in supervisor.events)

        await supervisor.stop_all()

    run(run_scenario())


def test_check_message_is_recognised_by_biz_type() -> None:
    """帧里带 bizType=40 的私信记录时要能被识别出来（用于解析失败告警）。"""
    assert carries_message(push_frame(encrypted_record()))
    assert not carries_message(push_frame(plain_record(AROUSE_PAYLOAD)))
    assert not carries_message({'headers': {'mid': 'x'}, 'code': 200})


def test_disabled_account_is_not_started(tmp_dir) -> None:
    async def run_scenario() -> None:
        supervisor, store, _ = make_supervisor(tmp_dir, enabled=False)
        await supervisor.start_enabled()
        account_id = store.list_accounts()[0].id
        assert supervisor.runtimes[account_id].status == 'stopped'
        assert supervisor.runtimes[account_id].task is None

    run(run_scenario())


def test_toggle_starts_and_stops(tmp_dir) -> None:
    async def run_scenario() -> None:
        supervisor, store, _ = make_supervisor(tmp_dir, enabled=False)
        account_id = store.list_accounts()[0].id

        account = store.update(account_id, enabled=True)
        await supervisor.apply_enabled(account)
        await wait_for(lambda: supervisor.runtimes[account_id].status == 'running')

        account = store.update(account_id, enabled=False)
        await supervisor.apply_enabled(account)
        assert supervisor.runtimes[account_id].status == 'stopped'

    run(run_scenario())


def test_account_with_incomplete_cookie_fails_without_retry(tmp_dir) -> None:
    async def run_scenario() -> None:
        store = Store(tmp_dir / 'a.json')
        account = store.add(name='残缺', cookie='tracknick=x', enabled=True)
        supervisor = Supervisor(store=store, notifier=RecordingNotifier())
        supervisor.start(account)

        runtime = supervisor.runtimes[account.id]
        await wait_for(lambda: runtime.status == 'error')
        assert 'cookie' in runtime.error
        assert runtime.task is None  # 不再重试
        assert any('cookie' in e.message for e in supervisor.events)

    run(run_scenario())


def test_expired_cookie_is_reported_with_a_way_out(tmp_dir) -> None:
    """Cookie 过期是最常见的一种启动失败：报错要直接说清楚该怎么处理，
    并且状态要落到 error（网页上才看得见），而不是只打一行日志。"""

    async def run_scenario() -> None:
        supervisor, store, _ = make_supervisor(tmp_dir)
        supervisor.live_factory = ExpiredCookieLive
        await supervisor.start_enabled()
        account_id = store.list_accounts()[0].id
        runtime = supervisor.runtimes[account_id]

        await wait_for(lambda: runtime.status == 'error')
        assert '登录态已失效' in runtime.error
        assert '重新扫码登录' in runtime.error
        assert any('登录态已失效' in e.message for e in supervisor.events)
        # 退避重试照旧：用户换上新的 Cookie 后不用重启进程就能自愈
        assert runtime.retry_count >= 1
        assert runtime.task is not None

        await supervisor.stop_all()

    run(run_scenario())


def test_rotated_cookie_is_written_back_to_the_config(tmp_dir) -> None:
    """长连接续期后轮换的 cookie 要落盘，且带节流（不会每 10 分钟写一次文件）。

    这是"跑久了提示获取 token 失败"的根因修复：轮换后的 token 以前只在内存里，
    断线重连/重启进程又拿配置里那份过期 cookie 去换 token，自然换不到。
    """

    async def run_scenario() -> None:
        supervisor, store, _ = make_supervisor(tmp_dir)
        account_id = store.list_accounts()[0].id
        fresh = 'unb=123456; tracknick=tester; _m_h5_tk=newtoken_1789235821040'
        runtime = supervisor.runtimes[account_id]

        supervisor._remember_session(account_id=account_id, cookie=fresh, expires_at=None)
        assert store.get(account_id).cookie == fresh
        assert runtime.account.cookie == fresh

        # 节流：紧接着的第二次上报不再写盘（值也不更新）
        supervisor._remember_session(account_id=account_id, cookie='unb=1; _m_h5_tk=x_1', expires_at=None)
        assert store.get(account_id).cookie == fresh

        # 值没变就不写（避免无意义落盘）
        supervisor._cookie_saved_at[account_id] = 0
        supervisor._remember_session(account_id=account_id, cookie=fresh, expires_at=None)
        assert store.get(account_id).cookie == fresh

    run(run_scenario())


def test_token_life_is_shown_and_expiry_is_warned_once(tmp_dir) -> None:
    """登录态剩余时间进运行态（界面要显示）；快到期时提醒一次（说明续期没成功）。"""

    async def run_scenario() -> None:
        supervisor, store, _ = make_supervisor(tmp_dir)
        account_id = store.list_accounts()[0].id
        runtime = supervisor.runtimes[account_id]
        token = f'unb=123456; tracknick=tester; _m_h5_tk=abc_{int((datetime.now(UTC) + timedelta(hours=2)).timestamp() * 1000)}'
        supervisor._remember_session(account_id=account_id, cookie=token, expires_at=None)

        assert runtime.token_expires_at is not None
        assert runtime.to_public()['token_life'].startswith('登录态剩余 1 小时')
        assert '登录态剩余' in runtime.to_template()['meta']
        assert not any('登录态将在' in e.message for e in supervisor.events)  # 还早，不提醒

        soon = datetime.now(UTC) + timedelta(minutes=10)
        supervisor._remember_session(account_id=account_id, cookie=token, expires_at=soon)
        supervisor._remember_session(account_id=account_id, cookie=token, expires_at=soon)
        warnings = [e for e in supervisor.events if '登录态将在' in e.message]
        assert len(warnings) == 1  # 同一个有效期只提醒一次
        assert warnings[0].level == 'warning'

    run(run_scenario())


def test_reconnect_uses_the_freshest_cookie_from_the_config(tmp_dir) -> None:
    """重连必须用配置里最新那份 cookie（用户改过、或上一轮回写过的）。"""

    async def run_scenario() -> None:
        supervisor, store, _ = make_supervisor(tmp_dir)
        account_id = store.list_accounts()[0].id
        factory = RecordingLiveFactory(fail_times=1)  # 第一次失败 → 退避后重连
        supervisor.live_factory = factory
        await supervisor.start_enabled()
        await wait_for(lambda: bool(factory.cookies), timeout=5)  # 第一次已经用旧 cookie 失败

        fresh = 'unb=123456; tracknick=tester; _m_h5_tk=fresh_1789235821040'
        store.update_cookie(account_id, fresh)
        await wait_for(lambda: len(factory.cookies) >= 2, timeout=15)

        assert factory.cookies[0] == GOOD_COOKIE
        assert factory.cookies[1] == fresh
        assert supervisor.runtimes[account_id].account.cookie.endswith('fresh_1789235821040')

        await supervisor.stop_all()

    run(run_scenario())


def test_failure_is_recorded_as_event_and_retried(tmp_dir) -> None:
    async def run_scenario() -> None:
        supervisor, store, _ = make_supervisor(tmp_dir)
        supervisor.live_factory = FailingLive
        await supervisor.start_enabled()
        account_id = store.list_accounts()[0].id

        runtime = supervisor.runtimes[account_id]
        await wait_for(lambda: runtime.status == 'error')
        assert 'boom' in runtime.error
        assert runtime.retry_count >= 1
        assert any(e.level == 'warning' for e in supervisor.events)

        await supervisor.stop_all()

    run(run_scenario())


# ── 事件广播（SSE 数据源） ────────────────────────────────────────────────────
def test_subscribers_receive_broadcasts(tmp_dir) -> None:
    async def run_scenario() -> None:
        supervisor, _store, _ = make_supervisor(tmp_dir)
        received: list[dict] = []
        unsubscribe = supervisor.subscribe(received.append)

        supervisor.publish_event(level='info', account_id='acc-1', message='测试事件')
        assert received[-1]['type'] == 'event'
        assert received[-1]['event']['message'] == '测试事件'

        unsubscribe()
        supervisor.publish_event(level='info', account_id='acc-1', message='不会再收到')
        assert len(received) == 1

    run(run_scenario())


def test_broken_subscriber_does_not_break_others(tmp_dir) -> None:
    async def run_scenario() -> None:
        supervisor, _, _ = make_supervisor(tmp_dir)
        received: list[dict] = []

        def boom(_: dict) -> None:
            raise RuntimeError('listener down')

        supervisor.subscribe(boom)
        supervisor.subscribe(received.append)
        supervisor.publish_event(level='info', account_id='acc-1', message='ok')
        assert len(received) == 1

    run(run_scenario())


def test_build_snapshot_shape(tmp_dir) -> None:
    async def run_scenario() -> None:
        supervisor, store, _ = make_supervisor(tmp_dir)
        snapshot = supervisor.build_snapshot()
        assert list(snapshot) == ['accounts', 'messages', 'events', 'notify']
        assert snapshot['accounts'][0]['display_name'] == '主力号'
        assert snapshot['accounts'][0]['status'] == 'stopped'
        assert 'cookie' not in snapshot['accounts'][0]
        # notify 段来自持久化配置，而不是推送器实例
        assert snapshot['notify'] == {
            'app_id': '',
            'app_secret_set': False,
            'chat_id': '',
            'chat_name': '',
            'has_app_credentials': False,
            'configured': False,
            'enabled': True,
            'chats': [],
            'chats_fetched_at': '',
            'chats_hint': '群列表还没缓存：填好应用 ID 与密钥后点「获取群列表」',
        }

        store.update_notify(app_id='cli_9', app_secret='s3cr3t!', chat_id='oc_9')
        store.update_notify_chats([{'chat_id': 'oc_9', 'name': '闲鱼消息汇总'}])
        assert supervisor.build_snapshot()['notify'] == {
            'app_id': 'cli_9',
            'app_secret_set': True,
            'chat_id': 'oc_9',
            'chat_name': '闲鱼消息汇总',
            'has_app_credentials': True,
            'configured': True,
            'enabled': True,
            'chats': [{'chat_id': 'oc_9', 'name': '闲鱼消息汇总', 'label': '闲鱼消息汇总（oc_9）'}],
            'chats_fetched_at': store.data.notify.chats_fetched_at.isoformat(timespec='seconds'),
            'chats_hint': store.data.notify.chats_hint,
        }
        assert 's3cr3t!' not in str(supervisor.build_snapshot())  # 密钥不进快照

    run(run_scenario())


def test_removed_account_runtime_is_dropped(tmp_dir) -> None:
    async def run_scenario() -> None:
        supervisor, store, _ = make_supervisor(tmp_dir, enabled=False)
        account_id = store.list_accounts()[0].id
        assert account_id in supervisor.runtimes

        store.remove(account_id)
        await supervisor.start_enabled()  # 内部会同步运行态
        assert account_id not in supervisor.runtimes

    run(run_scenario())


def test_message_buffer_is_capped(tmp_dir) -> None:
    async def run_scenario() -> None:
        from goofishpostman.supervisor import _MAX_MESSAGES, MessageRecord

        supervisor, _, _ = make_supervisor(tmp_dir)
        for index in range(_MAX_MESSAGES + 25):
            supervisor._append(
                buffer=supervisor.messages,
                item=MessageRecord(account_id='a', account_name='n', send_user_name='s', text=str(index)),
                limit=_MAX_MESSAGES,
            )
        assert len(supervisor.messages) == _MAX_MESSAGES
        assert supervisor.messages[-1].text == str(_MAX_MESSAGES + 24)

    run(run_scenario())


@mark.parametrize(argnames='field', argvalues=['unb', 'tracknick', '_m_h5_tk'])
def test_cookie_key_check_is_reported(tmp_dir, field: str) -> None:
    cookie = '; '.join(f'{key}=v' for key in ('unb', 'tracknick', '_m_h5_tk') if key != field)
    store = Store(tmp_dir / f'{field}.json')
    assert store.add(name='x', cookie=cookie).find_missing_cookie_keys() == [field]

"""多账号调度（Supervisor）的测试：全部用假的长连接，不联网。"""

from __future__ import annotations

from asyncio import get_running_loop, run, sleep
from datetime import UTC, datetime

from pytest import mark

from goofishpostman.goofish_utils import carries_message
from goofishpostman.store import Store
from goofishpostman.supervisor import MessageRecord, Supervisor, compute_next_backoff, format_time

from fixtures import AROUSE_PAYLOAD, encrypted_record, plain_record, push_frame
from helpers import GOOD_COOKIE, ConnectOnlyLive, FailingLive, FakeLive, RecordingNotifier


def make_supervisor(tmp_dir, *, enabled: bool = True) -> tuple[Supervisor, Store, RecordingNotifier]:
    store = Store(tmp_dir / 'a.json')
    store.add(name='主力号', cookie=GOOD_COOKIE, enabled=enabled)
    notifier = RecordingNotifier()
    supervisor = Supervisor(store, notifier)
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
        supervisor.publish_event('info', '', '飞书推送配置已更新')
        supervisor.publish_event('info', 'acct-1', '主力号 已连接')
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
    moment = datetime(2026, 9, 12, 4, 30, tzinfo=UTC)
    assert format_time(moment) == moment.astimezone().strftime('%m-%d %H:%M:%S')


def test_format_time_keeps_legacy_naive_values() -> None:
    """老配置文件里是不带时区的本地时间：原样显示，不做二次换算。"""
    assert format_time('2026-09-12T15:30:00') == '09-12 15:30:00'
    assert format_time('2026-09-12T15:30:00+00:00') == format_time(datetime(2026, 9, 12, 15, 30, tzinfo=UTC))
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
        supervisor = Supervisor(store, RecordingNotifier())
        supervisor.start(account)

        runtime = supervisor.runtimes[account.id]
        await wait_for(lambda: runtime.status == 'error')
        assert 'cookie' in runtime.error
        assert runtime.task is None  # 不再重试
        assert any('cookie' in e.message for e in supervisor.events)

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

        supervisor.publish_event('info', 'acc-1', '测试事件')
        assert received[-1]['type'] == 'event'
        assert received[-1]['event']['message'] == '测试事件'

        unsubscribe()
        supervisor.publish_event('info', 'acc-1', '不会再收到')
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
        supervisor.publish_event('info', 'acc-1', 'ok')
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
            'chat_id': '',
            'app_secret_set': False,
            'has_app_credentials': False,
            'configured': False,
            'enabled': True,
        }

        store.update_notify(app_id='cli_9', app_secret='sec', chat_id='oc_9')
        assert supervisor.build_snapshot()['notify'] == {
            'app_id': 'cli_9',
            'chat_id': 'oc_9',
            'app_secret_set': True,
            'has_app_credentials': True,
            'configured': True,
            'enabled': True,
        }

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
                supervisor.messages,
                MessageRecord(account_id='a', account_name='n', send_user_name='s', text=str(index)),
                _MAX_MESSAGES,
            )
        assert len(supervisor.messages) == _MAX_MESSAGES
        assert supervisor.messages[-1].text == str(_MAX_MESSAGES + 24)

    run(run_scenario())


@mark.parametrize('field', ['unb', 'tracknick', '_m_h5_tk'])
def test_cookie_key_check_is_reported(tmp_dir, field: str) -> None:
    cookie = '; '.join(f'{key}=v' for key in ('unb', 'tracknick', '_m_h5_tk') if key != field)
    store = Store(tmp_dir / f'{field}.json')
    assert store.add(name='x', cookie=cookie).find_missing_cookie_keys() == [field]

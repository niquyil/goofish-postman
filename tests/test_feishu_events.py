"""飞书回复事件 → 闲鱼私信 的解析与接线（不联网）。"""

from __future__ import annotations

from asyncio import run

from goofishpostman.feishu_events import FeishuReplyListener, extract_text, parse_incoming_message


class FakeSender:
    def __init__(self, sender_type: str = 'user') -> None:
        self.sender_type = sender_type


class FakeMessage:
    def __init__(
        self,
        *,
        content: str = '{"text":"好的，马上发"}',
        message_type: str = 'text',
        parent_id: str = 'om-card-1',
        root_id: str = 'om-card-1',
    ) -> None:
        self.content = content
        self.message_type = message_type
        self.parent_id = parent_id
        self.root_id = root_id
        self.message_id = 'om-reply-1'
        self.chat_id = 'oc_chat1'


class FakeEvent:
    def __init__(self, message: FakeMessage | None = None, sender_type: str = 'user') -> None:
        self.event = type('E', (), {'message': message, 'sender': FakeSender(sender_type)})()


def test_parse_text_reply() -> None:
    reply = parse_incoming_message(FakeEvent(FakeMessage()))
    assert reply is not None
    assert (reply.target, reply.text, reply.message_id) == ('om-card-1', '好的，马上发', 'om-reply-1')


def test_parse_strips_mention_placeholders() -> None:
    """群里回复机器人一般要 @ 它，正文里会留 @_user_1 这种占位符，得去掉。"""
    reply = parse_incoming_message(FakeEvent(FakeMessage(content='{"text":"@_user_1 有货的，可以直接拍"}')))
    assert reply is not None
    assert reply.text == '有货的，可以直接拍'


def test_parse_uses_root_id_when_parent_missing() -> None:
    reply = parse_incoming_message(FakeEvent(FakeMessage(parent_id='', root_id='om-root')))
    assert reply is not None
    assert reply.target == 'om-root'


def test_parse_keeps_a_message_without_quote_for_a_hint() -> None:
    """只 @ 了机器人、没引用卡片：target 为空，由上层提示怎么用。"""
    reply = parse_incoming_message(FakeEvent(FakeMessage(parent_id='', root_id='')))
    assert reply is not None
    assert reply.target == ''


def test_parse_ignores_non_text_and_bot_messages() -> None:
    assert parse_incoming_message(FakeEvent(FakeMessage(message_type='image'))) is None
    assert parse_incoming_message(FakeEvent(FakeMessage(content='{"text":"   "}'))) is None
    assert parse_incoming_message(FakeEvent(FakeMessage(), sender_type='bot')) is None
    assert parse_incoming_message(FakeEvent(None)) is None


def test_describe_event_makes_missing_delivery_obvious() -> None:
    """日志要能一眼看出「事件来了但没用」和「事件根本没来」的区别。"""
    from goofishpostman.feishu_events import describe_event

    line = describe_event(FakeEvent(FakeMessage()))
    assert 'type=text' in line
    assert '发送者=user' in line
    assert '引用=om-card-1' in line

    assert '不含消息体' in describe_event(FakeEvent(None))


def test_extract_text_handles_broken_content() -> None:
    assert extract_text('{"text":"在的"}') == '在的'
    assert extract_text('not json') == ''
    assert extract_text('{"image_key":"img_1"}') == ''
    assert extract_text('') == ''
    assert extract_text(None) == ''


def test_listener_without_credentials_does_not_start_a_thread() -> None:
    listener = FeishuReplyListener(app_id='', app_secret='', on_reply=lambda *_: None)
    listener.start()
    assert listener._thread is None


def test_listener_reports_ignored_events_to_the_log() -> None:
    """用不上的事件（非文本、机器人自己发的）也要进网页事件流，便于回查。"""
    ignored: list[str] = []

    class FakeLoop:
        def is_closed(self) -> bool:
            return False

    async def on_reply(*_args: str) -> None:  # pragma: no cover - 不该被调用
        raise AssertionError('这条事件不该被当成回复')

    async def on_ignored(detail: str) -> None:
        ignored.append(detail)

    listener = FeishuReplyListener(app_id='cli_1', app_secret='sec', on_reply=on_reply, on_ignored=on_ignored)
    listener.loop = FakeLoop()

    import goofishpostman.feishu_events as module

    original = module.run_coroutine_threadsafe
    module.run_coroutine_threadsafe = lambda coro, loop: run(coro)
    try:
        listener._on_event(FakeEvent(FakeMessage(message_type='image')))
        listener._on_event(FakeEvent(FakeMessage(), sender_type='bot'))
    finally:
        module.run_coroutine_threadsafe = original

    assert len(ignored) == 2
    assert all('type=' in detail for detail in ignored)


def test_listener_submits_the_reply_to_the_main_loop() -> None:
    """事件回调跑在长连接线程里，必须把活儿交回主事件循环。"""
    recorded: list[tuple[str, str, str]] = []

    class FakeLoop:
        closed = False

        def is_closed(self) -> bool:
            return False

        def call_soon_threadsafe(self, *_args) -> None:  # pragma: no cover - 不该走这条
            raise AssertionError('应该用 run_coroutine_threadsafe')

    async def on_reply(target: str, text: str, message_id: str) -> None:
        recorded.append((target, text, message_id))

    listener = FeishuReplyListener(app_id='cli_1', app_secret='sec', on_reply=on_reply)
    listener.loop = FakeLoop()
    submitted: list[object] = []

    import goofishpostman.feishu_events as module

    original = module.run_coroutine_threadsafe

    def fake_submit(coro, loop):
        submitted.append((coro, loop))
        run(coro)  # 直接在当前循环里跑，省掉线程

    module.run_coroutine_threadsafe = fake_submit
    try:
        listener._on_event(FakeEvent(FakeMessage()))
    finally:
        module.run_coroutine_threadsafe = original

    assert recorded == [('om-card-1', '好的，马上发', 'om-reply-1')]
    assert submitted and isinstance(submitted[0][1], FakeLoop)

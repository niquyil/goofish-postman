"""多账号并发管理：每个账号一个长连接任务，收到的私信汇总推送到飞书。"""

from __future__ import annotations

from asyncio import CancelledError, Task, create_task, get_running_loop, sleep
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from .accounts import FeishuNotifier, NotifyError
from .goofish_live import GoofishLive, extract_message_text
from .goofish_utils import extract_message_time, extract_message_uid, format_time, is_silent_message
from .sender import pick_header_color
from .store import Account, Store

if TYPE_CHECKING:
    from collections.abc import Callable

    from websockets import ClientConnection

    from .types import MessageInfo


AccountStatus = Literal['stopped', 'starting', 'running', 'error']

# 状态的中文文案与样式，模板与前端共用（改这里即可）
STATUS_TEXT = {'running': '监听中', 'starting': '连接中', 'stopped': '已停止', 'error': '异常'}
STATUS_CLASS = {'running': 'pill-ok', 'starting': 'pill-warn', 'error': 'pill-err', 'stopped': 'pill-off'}

# 断线重连的退避区间
_RETRY_MIN = 2.0
_RETRY_MAX = 60.0
# 连接存活不足这个秒数就断开，视为连接不稳定，退避翻倍
_SHORT_LIVED = 30.0
# 界面消息流保留条数
_MAX_MESSAGES = 200
_MAX_EVENTS = 200
# 去重表保留的消息 id 数量（防止无限增长）
_MAX_SEEN_MESSAGES = 2000
# 会话 → 商品标题的映射保留条数
_MAX_SESSION_TITLES = 500


def compute_next_backoff(current: float, alive_for: float, healthy: bool) -> float:
    """算下一次重连要等多久。

    - 连接稳定过（收到过消息，或存活超过 _SHORT_LIVED）→ 立刻重连
    - 连上就断（大概率是 cookie 失效 / 被限流）→ 退避翻倍，避免打爆接口
    """
    if healthy or alive_for >= _SHORT_LIVED:
        return _RETRY_MIN
    return min(max(current, _RETRY_MIN) * 2, _RETRY_MAX)


@dataclass
class MessageRecord:
    account_id: str
    account_name: str
    send_user_name: str
    text: str
    # 接收方展示成「昵称(账号)」；发送方即 send_user_name
    receiver: str = ''
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def sender(self) -> str:
        """发送方昵称（报文里提醒标题给的名字）。"""
        return self.send_user_name

    def to_public(self) -> dict[str, Any]:
        return {
            'account_id': self.account_id,
            'account_name': self.account_name,
            'send_user_name': self.send_user_name,
            'sender': self.sender,
            'receiver': self.receiver or self.account_name,
            'text': self.text,
            'at': self.at.isoformat(timespec='seconds'),
            'at_text': format_time(self.at),
        }


@dataclass
class EventRecord:
    level: Literal['info', 'warning', 'error']
    account_id: str
    message: str
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_public(self) -> dict[str, Any]:
        return {
            'level': self.level,
            'account_id': self.account_id,
            'message': self.message,
            'at': self.at.isoformat(timespec='seconds'),
            'at_text': format_time(self.at),
        }


@dataclass
class AccountRuntime:
    """某个账号的运行态（供界面展示）。"""

    account: Account
    status: AccountStatus = 'stopped'
    error: str = ''
    started_at: datetime | None = None
    message_count: int = 0
    retry_count: int = 0
    last_message_at: datetime | None = None
    task: Task | None = None

    def to_public(self) -> dict[str, Any]:
        return {
            **self.account.to_public(),
            'status': self.status,
            'error': self.error,
            'started_at': self.started_at.isoformat(timespec='seconds') if self.started_at else None,
            'message_count': self.message_count,
            'retry_count': self.retry_count,
            'last_message_at': self.last_message_at.isoformat(timespec='seconds') if self.last_message_at else None,
        }

    def to_template(self) -> dict[str, Any]:
        """给 Jinja 模板用的视图：在 to_public 基础上补齐展示文案。"""
        public = self.to_public()
        meta = []
        if public['has_cookie']:
            meta.append(f'Cookie {public["cookie_hint"]}')
        meta.append(f'收到 {public["message_count"]} 条')
        if public['last_message_at']:
            meta.append(f'最后 {format_time(public["last_message_at"])}')
        if public['retry_count']:
            meta.append(f'重连 {public["retry_count"]} 次')

        problem = self.error
        if not problem and public['missing_cookie_keys']:
            problem = f'Cookie 缺少字段: {", ".join(public["missing_cookie_keys"])}'

        return {
            **public,
            'status_text': STATUS_TEXT.get(self.status, self.status),
            'status_class': STATUS_CLASS.get(self.status, 'pill-off'),
            'meta': ' · '.join(meta),
            'problem': problem,
        }


class Supervisor:
    """账号监听的总调度。

    - `start_enabled()` 拉起所有 enabled 且 cookie 完整的账号
    - 每个账号由独立任务负责，断线自动重连（指数退避）
    - 收到的私信先写进内存消息流，再推送飞书
    """

    def __init__(self, store: Store, notifier: FeishuNotifier | None = None) -> None:
        self.store = store
        self.notifier = notifier or FeishuNotifier()
        # 可替换：测试与自定义场景传入自己的构造器
        self.live_factory: Callable[[str], GoofishLive] = GoofishLive
        self.runtimes: dict[str, AccountRuntime] = {}
        self.messages: list[MessageRecord] = []
        self.events: list[EventRecord] = []
        # 已推送过的消息 id（按账号分别记录）：同一条私信会随多个帧重复下发，必须去重
        self._seen_messages: dict[str, OrderedDict[str, None]] = {}
        # 会话 → 商品标题（从会话/预热记录里学到，转发时写进卡片明细）
        self._session_titles: dict[str, str] = {}
        self._listeners: list[Callable[[dict], None]] = []
        self.sync_runtimes()

    # ── 生命周期 ──────────────────────────────────────────────────────────────
    def sync_runtimes(self) -> None:
        """让运行态跟着配置走：新增的登记进来，删掉的清理掉。"""
        for account in self.store.list_accounts():
            self.runtimes.setdefault(account.id, AccountRuntime(account=account))
        known = {a.id for a in self.store.list_accounts()}
        for account_id in list(self.runtimes):
            if account_id not in known:
                runtime = self.runtimes.pop(account_id)
                if runtime.task is not None:
                    runtime.task.cancel()

    async def start_enabled(self) -> None:
        self.sync_runtimes()
        for account in self.store.list_accounts():
            if account.enabled:
                self.start(account)

    def start(self, account: Account) -> None:
        """启动（或重启）某个账号的监听任务（只创建任务，不等待连接建立）。"""
        self.sync_runtimes()
        runtime = self.runtimes.get(account.id)
        if runtime is None:
            runtime = self.runtimes[account.id] = AccountRuntime(account=account)
        if runtime.task is not None and not runtime.task.done():
            return
        runtime.account = account
        runtime.status = 'starting'
        runtime.error = ''
        runtime.retry_count = 0
        runtime.task = create_task(self._run_account(account), name=f'goofish-{account.id}')

    async def stop(self, account_id: str) -> None:
        runtime = self.runtimes.get(account_id)
        if runtime is None or runtime.task is None:
            return
        task, runtime.task = runtime.task, None
        task.cancel()
        try:
            await task
        except CancelledError:
            pass
        self._set_status(runtime, 'stopped')
        self.publish_event('info', account_id, f'{runtime.account.display_name} 已停止监听')

    async def stop_all(self) -> None:
        for account_id in list(self.runtimes):
            await self.stop(account_id)
        await self.notifier.close()

    # ── 账号任务 ──────────────────────────────────────────────────────────────
    async def _run_account(self, account: Account) -> None:
        """连接 → 监听；异常时按退避重连，直到任务被取消。"""
        runtime = self.runtimes[account.id]
        backoff = _RETRY_MIN
        while True:
            healthy = False
            started = get_running_loop().time()
            try:
                live = self.live_factory(account.cookie)
            except KeyError as e:
                # cookie 里缺 unb，重连也没用
                self._fail(runtime, f'cookie 缺少 {e} 字段，请重新复制登录后的完整 cookie')
                return

            def publish_connected(_runtime: AccountRuntime = runtime) -> None:
                """连接建立时上报状态与事件（网页上要立刻能看到"已连接"）。

                不参与重连退避判断：只握手成功不算健康，否则遇到"连上就被踢"的
                cookie 会退化成不停重连。
                """
                if _runtime.status == 'running':
                    return
                _runtime.status = 'running'
                _runtime.error = ''
                _runtime.retry_count = 0
                if _runtime.started_at is None:
                    _runtime.started_at = datetime.now(UTC)
                self.publish_event('info', account.id, f'{account.display_name} 已连接，开始监听')
                self._broadcast({'type': 'account', 'account': _runtime.to_public()})

            def mark_healthy() -> None:
                """收到过消息才算连接健康（退避策略用），同时兼容不回调 on_connected 的假实现。"""
                nonlocal healthy
                healthy = True
                publish_connected()

            live.on_connected = publish_connected
            # 会话/预热记录里带商品标题，先记下来，转发消息时写进卡片明细
            live.on_session_info = self._remember_session_title
            live.handle_message = self._make_handler(account, runtime, mark_healthy)
            try:
                await live.main()
                reason = '连接已关闭'
            except CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 - 任何异常都走重连
                reason = f'{type(e).__name__}: {e}'

            backoff = compute_next_backoff(backoff, alive_for=get_running_loop().time() - started, healthy=healthy)
            runtime.retry_count += 1
            runtime.status = 'error'
            runtime.error = reason
            self.publish_event('warning', account.id, f'{account.display_name} {reason}，{backoff:.0f}s 后重连')
            self._broadcast({'type': 'account', 'account': runtime.to_public()})
            await sleep(backoff)

    def _make_handler(self, account: Account, runtime: AccountRuntime, on_message: Callable[[], None]):
        async def handle_message(message, websocket: ClientConnection) -> None:
            on_message()
            # 报文带的是发送方昵称，顺手学下来更新账号昵称与对照表；
            # 然后重新从 store 读一次账号，让展示用上刚学到的昵称
            self._learn_nickname(message)
            current = self.store.get(account.id) or account
            if self.is_self_sent(current, message['send_user_id'], message['send_user_name']):
                # 这条是当前监听的账号自己发出去的：账号互发时双方的连接都会收到它，
                # 发送方那一侧不该当成"收到消息"推给飞书
                return
            uid = extract_message_uid(message['raw'])
            if uid and self._is_duplicate(current.id, uid):
                # 同一条私信会随多个帧重复下发，重复的只更新心跳、不再推送
                runtime.last_message_at = datetime.now(UTC)
                return
            record = MessageRecord(
                account_id=current.id,
                account_name=current.display_name,
                send_user_name=message['send_user_name'] or self._resolve_sender_name(message['send_user_id']),
                text=extract_message_text(message),
                receiver=current.label,
            )
            runtime.message_count += 1
            runtime.last_message_at = record.at
            self._append(self.messages, record, _MAX_MESSAGES)
            self._broadcast({'type': 'message', 'message': record.to_public()})
            self._broadcast({'type': 'account', 'account': runtime.to_public()})
            if is_silent_message(message['raw']):
                # 平台提示条这类噪音只留在网页消息流里，不推飞书
                return
            try:
                await self.notifier.send_account_message(
                    current.label,
                    message,
                    record.text,
                    sender=record.sender,
                    details=self._build_message_details(message),
                    color=pick_header_color(current.id),
                )
            except NotifyError as e:
                self.publish_event('error', current.id, str(e))

        return handle_message

    def _build_message_details(self, message) -> dict[str, str]:
        """卡片明细：只要消息发生时间与商品名（取不到的字段不显示）。"""
        details: dict[str, str] = {}
        created = extract_message_time(message['raw'])
        if created:
            details['时间'] = created
        title = self._session_titles.get(message['cid'])
        if title:
            details['商品'] = title if len(title) <= 30 else f'{title[:30]}…'
        return details

    def _remember_session_title(self, session_id: str, title: str) -> None:
        """记住「会话 → 商品标题」（从会话/预热记录里学，卡片上用）。"""
        self._session_titles[session_id] = title
        if len(self._session_titles) > _MAX_SESSION_TITLES:
            self._session_titles.pop(next(iter(self._session_titles)))

    # ── 账号身份（昵称 ↔ 账号 id） ─────────────────────────────────────────────
    def build_account_map(self) -> dict[str, str]:
        """昵称/备注名 → 账号 id 的对照表。

        报文里带的是发送方 id（senderUserId）与昵称（提醒标题），
        这张表用来判断某条消息的发送方对应哪个账号。
        """
        mapping: dict[str, str] = {}
        for account in self.store.list_accounts():
            for name in account.aliases:
                mapping[name] = account.unb
        return mapping

    def _resolve_sender_name(self, sender_id: str) -> str:
        """报文没给发送方昵称时，用对照表按账号 id 反查。"""
        if not sender_id:
            return ''
        for account in self.store.list_accounts():
            if account.unb == sender_id:
                return account.nickname or account.display_name
        return sender_id

    def _learn_nickname(self, message: MessageInfo) -> None:
        """从报文里学真实昵称并缓存到本地（报文的提醒标题就是发送方昵称）。

        每次收到消息都比对一次：昵称变了就更新缓存并记事件，不变则什么都不做
        （不落盘）。cookie 里的 tracknick 可能是登录时的旧值，所以：
        - cookie 值看起来不可信（等于账号 id / 备注名）时，报文昵称直接覆盖；
        - 否则仅在昵称确实变化时更新。
        """
        sender_id = message.get('send_user_id') or ''
        sender_name = (message.get('send_user_name') or '').strip()
        if not sender_id or not sender_name or sender_name == sender_id:
            return
        target = next((a for a in self.store.list_accounts() if a.unb == sender_id), None)
        if target is None or target.nickname_override == sender_name:
            return
        # 只有当缓存的昵称确实"过时/不可信"或确实变化时才更新
        if target.nickname_override and target.nickname_override == sender_name:
            return
        updated = self.store.set_nickname(target.id, sender_name)
        if updated is None:
            return
        logger.info(f'{updated.display_name} 昵称更新为 {sender_name}')
        self.publish_event('info', updated.id, f'{updated.display_name} 昵称更新为 {sender_name}')

    @staticmethod
    def is_self_sent(account: Account, sender_id: str, sender_name: str) -> bool:
        """这条消息是不是由当前监听的账号自己发出的。

        优先比对闲鱼账号 id；id 取不到时退回昵称/备注名比对。
        """
        if sender_id:
            # 有 id 就以 id 为准：对不上就是别的账号发的，不再用名字猜
            return bool(account.unb) and sender_id == account.unb
        return bool(sender_name) and sender_name in account.aliases

    def _is_duplicate(self, account_id: str, uid: str) -> bool:
        """该账号是否已经推送过这条消息（按账号分别去重）。

        账号互发消息时，同一条消息会同时出现在双方的连接上，
        但它对每个账号都是"自己收到的消息"，所以各自推一条、各自归属自己。
        若改成全局去重，就会变成"谁先处理就归谁"，归属随连接顺序漂移。
        """
        seen = self._seen_messages.setdefault(account_id, OrderedDict())
        if uid in seen:
            return True
        seen[uid] = None
        while len(seen) > _MAX_SEEN_MESSAGES:
            seen.popitem(last=False)
        return False

    def _fail(self, runtime: AccountRuntime, message: str) -> None:
        runtime.status = 'error'
        runtime.error = message
        runtime.task = None
        self.publish_event('error', runtime.account.id, f'{runtime.account.display_name} {message}')
        self._broadcast({'type': 'account', 'account': runtime.to_public()})

    def _set_status(self, runtime: AccountRuntime, status: AccountStatus) -> None:
        runtime.status = status
        if status == 'stopped':
            runtime.started_at = None
        self._broadcast({'type': 'account', 'account': runtime.to_public()})

    # ── 账号配置变更时同步运行态 ──────────────────────────────────────────────
    async def apply_enabled(self, account: Account) -> None:
        """按最新的开关状态启动或停止该账号。"""
        if account.enabled and not account.find_missing_cookie_keys():
            self.start(account)
        else:
            await self.stop(account.id)

    async def restart(self, account_id: str) -> None:
        await self.stop(account_id)
        account = self.store.get(account_id)
        if account is not None and account.enabled:
            self.start(account)

    # ── 事件与消息流 ──────────────────────────────────────────────────────────
    @staticmethod
    def _append(buffer: list, item: Any, limit: int) -> None:
        buffer.append(item)
        if len(buffer) > limit:
            del buffer[: len(buffer) - limit]

    def publish_event(self, level: Literal['info', 'warning', 'error'], account_id: str, message: str) -> None:
        logger.log(level.upper(), f'[{account_id}] {message}')
        event = EventRecord(level=level, account_id=account_id, message=message)
        self._append(self.events, event, _MAX_EVENTS)
        self._broadcast({'type': 'event', 'event': event.to_public()})

    def subscribe(self, listener: Callable[[dict], None]) -> Callable[[], None]:
        """注册 SSE 监听器，返回取消订阅的函数。"""
        self._listeners.append(listener)

        def unsubscribe() -> None:
            if listener in self._listeners:
                self._listeners.remove(listener)

        return unsubscribe

    def _broadcast(self, payload: dict) -> None:
        for listener in list(self._listeners):
            try:
                listener(payload)
            except Exception as e:  # noqa: BLE001 - 单个监听器出错不影响其它
                logger.debug(f'监听器异常: {e}')

    def build_snapshot(self) -> dict[str, Any]:
        return {
            'accounts': [runtime.to_public() for runtime in self.runtimes.values()],
            'messages': [m.to_public() for m in self.messages],
            'events': [e.to_public() for e in self.events],
            'notify': {
                'uuid': self.store.data.notify.uuid,
                'configured': self.store.data.notify.configured,
                'enabled': self.store.data.notify.enabled,
            },
        }

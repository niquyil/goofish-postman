"""账号与配置的本地持久化。

文件默认落在 `path.DATA_FILE`（Windows: %APPDATA%\\GoofishPostman\\accounts.json），
包含闲鱼 Cookie 等敏感信息，写入时按 0600 收紧权限，并且原子替换。
"""

from __future__ import annotations

from datetime import UTC, datetime
from json import JSONDecodeError, dumps, loads
from os import chmod, replace
from os import name as os_name  # add(name=...) 参数会遮蔽裸 name，故导入时改名
from pathlib import Path
from threading import Lock
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator

from .path import DATA_FILE

# Cookie 里必须存在的字段：unb 是账号 id，缺了连不上
REQUIRED_COOKIE_KEYS = ('unb', 'tracknick', '_m_h5_tk')
# 飞书卡片 → 闲鱼会话 的对照表保留条数（防止无限增长）
_MAX_MESSAGE_LINKS = 500


class Account(BaseModel):
    """一个闲鱼账号的登录凭据与监听开关。"""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    name: str = ''
    cookie: str = ''
    enabled: bool = True
    # 从报文里学到的真实昵称（cookie 的 tracknick 可能是旧值）
    nickname_override: str = ''
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @field_validator('cookie')
    @classmethod
    def _strip_cookie(cls, value: str) -> str:
        return value.strip()

    @field_validator('name')
    @classmethod
    def _strip_name(cls, value: str) -> str:
        return value.strip()

    @property
    def display_name(self) -> str:
        return self.name or '未命名账号'

    @property
    def unb(self) -> str:
        """闲鱼账号 id（cookie 里的 unb），报文中以 senderUserId / 接收方出现。"""
        return self.get_cookie_value('unb')

    @property
    def cookie_nickname(self) -> str:
        """cookie 里的 tracknick（可能是过期值）。"""
        return self.get_cookie_value('tracknick')

    @property
    def cookie_nickname_looks_stale(self) -> bool:
        """cookie 里的昵称不可信：等于账号 id、或等于备注名（多半是登录时的旧值）。"""
        tracknick = self.cookie_nickname
        if not tracknick:
            return True
        return tracknick in {self.unb, self.name}

    @property
    def nickname(self) -> str:
        """账号昵称：优先用报文里学到的（缓存在本地），否则退回 cookie 的 tracknick。"""
        return self.nickname_override or self.cookie_nickname

    @property
    def aliases(self) -> set[str]:
        """这个账号的所有可识别名字：备注名 + 闲鱼昵称。"""
        return {name for name in (self.name, self.nickname) if name}

    @property
    def label(self) -> str:
        """展示用的「昵称(备注名)」，例如 网课学习私人助理(kisuke)。

        用备注名而不是数字账号，方便和网页上的账号卡片对上。
        昵称与备注名相同（cookie 里常是旧值）或昵称为空时只显示一个，
        避免出现 kisuke(kisuke) 这种冗余。
        """
        if not self.name:
            return self.nickname or self.display_name
        if not self.nickname or self.nickname == self.name:
            return self.name
        return f'{self.nickname}({self.name})'

    def get_cookie_value(self, key: str) -> str:
        for part in self.cookie.split(';'):
            found_key, _, value = part.strip().partition('=')
            if found_key == key:
                return value.strip()
        return ''

    def find_missing_cookie_keys(self) -> list[str]:
        if not self.cookie:
            return list(REQUIRED_COOKIE_KEYS)
        return [key for key in REQUIRED_COOKIE_KEYS if f'{key}=' not in self.cookie]

    def is_usable(self) -> bool:
        return self.enabled and not self.find_missing_cookie_keys()

    def mask_cookie(self) -> str:
        """给界面看的脱敏串，不暴露完整凭据。"""
        if not self.cookie:
            return ''
        tail = self.cookie[-4:] if len(self.cookie) > 4 else ''
        return f'***{tail}'

    def to_public(self) -> dict[str, Any]:
        """对外（HTTP 响应 / 前端）展示用，绝不包含完整 cookie。"""
        return {
            'id': self.id,
            'name': self.name,
            'display_name': self.display_name,
            'nickname': self.nickname,
            'label': self.label,
            'enabled': self.enabled,
            'has_cookie': bool(self.cookie),
            'cookie_hint': self.mask_cookie(),
            'missing_cookie_keys': self.find_missing_cookie_keys(),
            'created_at': self.created_at.isoformat(timespec='seconds'),
            'updated_at': self.updated_at.isoformat(timespec='seconds'),
        }


class WebSettings(BaseModel):
    host: str = '127.0.0.1'
    port: int = 8848
    # 留空则不校验；一旦设置，所有接口都要求这个令牌
    token: str = ''


class NotifySettings(BaseModel):
    """飞书推送配置（企业自建应用）。

    消息由应用机器人发到 `chat_id` 指定的群；`app_id`/`app_secret` 另外还用于上传图片
    （换 image_key 才能把图片内嵌进卡片）和列群。三项缺任意一项都不发送。
    """

    app_id: str = ''
    app_secret: str = ''
    # 发送目标（群 id，形如 oc_xxx）；机器人必须已经在这个群里
    chat_id: str = ''
    enabled: bool = True

    @property
    def has_app_credentials(self) -> bool:
        return bool(self.app_id.strip() and self.app_secret.strip())

    @property
    def configured(self) -> bool:
        """能发消息的前提：应用凭据 + 目标群。"""
        return bool(self.has_app_credentials and self.chat_id.strip())


class MessageLink(BaseModel):
    """飞书卡片 → 闲鱼会话 的对应关系。

    在飞书里「回复」机器人发的那张卡片时，事件里带的是被回复消息的 message_id，
    靠这张表才能找回「用哪个账号、发到哪个会话、发给谁」。落盘保留，
    服务重启后旧卡片照样能回。
    """

    account_id: str
    cid: str
    toid: str
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class StoreData(BaseModel):
    accounts: list[Account] = Field(default_factory=list)
    web: WebSettings = Field(default_factory=WebSettings)
    notify: NotifySettings = Field(default_factory=NotifySettings)
    # 飞书消息 id → 闲鱼会话（只在回复功能里用；条数有上限）
    message_links: dict[str, MessageLink] = Field(default_factory=dict)


class Store:
    """accounts.json 的读写入口；所有写操作都会落盘。"""

    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path) if path else DATA_FILE
        self._lock = Lock()
        self.data = self._read()

    # ── 读写 ──────────────────────────────────────────────────────────────────
    def _read(self) -> StoreData:
        if not self.path.exists():
            return StoreData()
        try:
            raw = loads(self.path.read_text(encoding='utf-8'))
        except (JSONDecodeError, OSError) as e:
            raise RuntimeError(f'配置文件损坏: {self.path} ({e})') from e
        return StoreData.model_validate(raw)

    def save(self) -> None:
        """原子写入，并尽量把权限收紧到 0600。"""
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = dumps(self.data.model_dump(mode='json'), ensure_ascii=False, indent=2)
            tmp = self.path.with_suffix(f'{self.path.suffix}.tmp')
            with open(tmp, 'w', encoding='utf-8') as f:
                f.write(payload)
            if os_name == 'posix':
                chmod(tmp, 0o600)
            replace(tmp, self.path)

    # ── 账号 ──────────────────────────────────────────────────────────────────
    def list_accounts(self) -> list[Account]:
        return list(self.data.accounts)

    def get(self, account_id: str) -> Account | None:
        return next((a for a in self.data.accounts if a.id == account_id), None)

    def add(self, name: str, cookie: str, enabled: bool = True) -> Account:
        account = Account(name=name, cookie=cookie, enabled=enabled)
        self.data.accounts.append(account)
        self.save()
        return account

    def update(self, account_id: str, **changes: Any) -> Account:
        account = self.get(account_id)
        if account is None:
            raise KeyError(account_id)
        updates = {key: value for key, value in changes.items() if value is not None}
        updates['updated_at'] = datetime.now(UTC)
        updated = account.model_copy(update=updates)
        self.data.accounts[self.data.accounts.index(account)] = updated
        self.save()
        return updated

    def update_cookie(self, account_id: str, cookie: str) -> Account:
        return self.update(account_id, cookie=cookie)

    def set_nickname(self, account_id: str, nickname: str) -> Account | None:
        """记下从报文里学到的真实昵称（缓存在本地，cookie 里的 tracknick 可能是旧值）。

        只回填昵称字段，不覆盖整条 cookie，因此不会动登录态；
        昵称没变化时不落盘，避免每条消息都写文件。
        """
        account = self.get(account_id)
        nickname = (nickname or '').strip()
        # 注意比的是 nickname_override（实际存下来的字段），不是派生属性 nickname
        if account is None or not nickname or account.nickname_override == nickname:
            return account
        updated = account.model_copy(update={'nickname_override': nickname, 'updated_at': datetime.now(UTC)})
        self.data.accounts[self.data.accounts.index(account)] = updated
        self.save()
        return updated

    def clear_nickname(self, account_id: str) -> Account | None:
        """清掉缓存的昵称，下次收到该账号的消息时会重新学习。"""
        account = self.get(account_id)
        if account is None or not account.nickname_override:
            return account
        updated = account.model_copy(update={'nickname_override': '', 'updated_at': datetime.now(UTC)})
        self.data.accounts[self.data.accounts.index(account)] = updated
        self.save()
        return updated

    def remove(self, account_id: str) -> None:
        account = self.get(account_id)
        if account is None:
            raise KeyError(account_id)
        self.data.accounts.remove(account)
        self.save()

    # ── 飞书卡片 → 闲鱼会话 ────────────────────────────────────────────────────
    def remember_message_link(self, message_id: str, account_id: str, cid: str, toid: str) -> None:
        """记下「这条飞书消息是从哪个闲鱼会话发出来的」，回复时要用。"""
        if not message_id:
            return
        self.data.message_links[message_id] = MessageLink(account_id=account_id, cid=cid, toid=toid)
        while len(self.data.message_links) > _MAX_MESSAGE_LINKS:
            # 字典保持插入顺序，先记的先淘汰（老卡片不太可能再被回复）
            self.data.message_links.pop(next(iter(self.data.message_links)))
        self.save()

    def get_message_link(self, message_id: str) -> MessageLink | None:
        return self.data.message_links.get(message_id)

    # ── 配置 ──────────────────────────────────────────────────────────────────
    def update_web(self, **changes: Any) -> WebSettings:
        updates = {key: value for key, value in changes.items() if value is not None}
        self.data.web = self.data.web.model_copy(update=updates)
        self.save()
        return self.data.web

    def update_notify(self, **changes: Any) -> NotifySettings:
        updates = {key: value for key, value in changes.items() if value is not None}
        self.data.notify = self.data.notify.model_copy(update=updates)
        self.save()
        return self.data.notify

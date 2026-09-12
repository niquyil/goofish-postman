"""闲鱼协议的本地实现。

原来这些逻辑靠 `script/goofish.js`（execjs 调 node）完成，现已全部改成 Python，
`script/goofish.js` 也已删除：

- `generate_mid` / `generate_uuid` / `generate_device_id` / `generate_sign`：几行等价实现
- `decrypt`：goofish.js 里那 500 行是**被混淆的 MessagePack 解码器**，
  直接用 msgpack 库取代（见 `decode_messagepack`），
  已用它注释里自带的真实样本逐字段比对一致（样本留在 `tests/data/`）

因此运行时不再需要 Node.js；只有 `script/tfstk.js` 取 tfstk cookie 那一步
仍会调 node，且拿不到也不影响登录（见 `goofish_apis.generate_tfstk`）。
"""

from __future__ import annotations

from base64 import b64decode
from datetime import UTC, datetime
from hashlib import md5
from json import JSONDecodeError, dumps, loads
from random import randint
from typing import TYPE_CHECKING, Any

from loguru import logger
from msgpack import unpackb

from .types import MTOP_APP_KEY, MessageInfo

if TYPE_CHECKING:
    from collections.abc import Iterator

# JS 里 device_id 用的字符表（注意末尾是 - 和 _，共 64 个）
DEVICE_ID_ALPHABET = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_'


def generate_mid() -> str:
    """消息 id：3 位随机数 + 13 位毫秒时间戳 + ' 0'。"""
    return f'{randint(0, 999)}{int(datetime.now(UTC).timestamp() * 1000)} 0'


def generate_uuid() -> str:
    """长连接要的 uuid：'-' + 毫秒时间戳 + '1'。"""
    return f'-{int(datetime.now(UTC).timestamp() * 1000)}1'


def generate_device_id(user_id: str | None = None) -> str:
    """设备指纹。

    与 goofish.js 一致：生成 UUID v4 形状的 36 位串
    （第 8/13/18/23 位是 '-'，第 14 位固定 '4'，第 19 位带 variant 位），
    后面拼上 '-' + user_id。扫码登录时还没有 user_id，只保留随机部分。
    """
    chars = []
    for index in range(36):
        if index in (8, 13, 18, 23):
            chars.append('-')
        elif index == 14:
            chars.append('4')
        else:
            nibble = randint(0, 15)
            chars.append(DEVICE_ID_ALPHABET[(3 & nibble) | 8 if index == 19 else nibble])
    return ''.join(chars) + '-' + (user_id or '')


def generate_sign(timestamp: str, token: str, data: str) -> str:
    """mtop 签名：md5(token&t&appKey&data)。"""
    return md5(f'{token}&{timestamp}&{MTOP_APP_KEY}&{data}'.encode()).hexdigest()


def decode_messagepack(raw: bytes | str) -> Any:
    """解码闲鱼消息体（MessagePack）。

    对应 goofish.js 里的 `decrypt`：它的实现是一段被混淆的 MessagePack 解码器，
    行为与 msgpack 库一致（已用真实样本比对）。`strict_map_key=False`
    是为了先接住整数键的报文（线上有 `{1: {10: ...}}` 这种写法），
    随后再统一转成字符串键。

    键统一转成字符串：原来的 JS 实现最后过了一遍 `JSON.stringify`，
    数字键会被强制变成字符串，下游（`find_message_body` 等）都按字符串键取值。
    值保持原类型（数字不转字符串），比 JS 更准确。
    """
    if isinstance(raw, str):
        raw = b64decode(raw)
    return _stringify_keys(unpackb(raw, raw=False, strict_map_key=False))


def _stringify_keys(value: Any) -> Any:
    """递归把 dict 的键统一成字符串（等价于 JS `JSON.stringify` 的键处理）。

    整数键的报文（如 goofish.js 里留的旧样本）不转换就取不到值：
    `payload['1']` 会 KeyError，整条消息被当成"没有正文字段"丢掉。
    用显式栈而不是递归，避免深报文触发 RecursionError。
    """
    stack = [value]
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if any(not isinstance(key, str) for key in node):
                items = [(key if isinstance(key, str) else str(key), item) for key, item in node.items()]
                node.clear()
                node.update(items)
            stack.extend(node.values())
        elif isinstance(node, list):
            stack.extend(node)
    return value


def decrypt(string: str) -> str:
    """兼容入口：把密文解成 JSON 字符串（原 goofish.js 的返回形式）。"""
    return dumps(decode_messagepack(string), ensure_ascii=False, default=str)


def decrypt_data(string: str) -> dict:
    """解一条推送记录。线上有三种形态，都要认：

    1. 明文 JSON（少数内联数据）
    2. base64 的明文 JSON —— 会话、预热等记录（`bizType=370`）
    3. base64 的 MessagePack —— 私信记录（`bizType=40`）

    路径 2 不能漏：漏了会把绝大多数会话记录判成"解不开"而静默跳过。
    """
    try:
        return loads(string)
    except JSONDecodeError:
        pass

    try:
        decoded = b64decode(string, validate=True).decode('utf-8')
    except Exception:  # noqa: BLE001 - 不是 base64 或不是 UTF-8，转 MessagePack
        return decode_messagepack(string)

    try:
        return loads(decoded)
    except JSONDecodeError:
        return decode_messagepack(string)


def _extract_records(message: dict) -> list | None:
    """取出长连接推送里的记录列表。

    真实帧形如：
      {'lwp': '/s/sync', 'body': {'syncPushPackage': {'data': [ {'bizType':..,'data':..}, ... ]}}}
    `/s/vulcan`、`/s/sync`、`/s/para` 都可能带 syncPushPackage。
    """
    body = message.get('body')
    if isinstance(body, dict):
        package = body.get('syncPushPackage')
        if isinstance(package, dict) and isinstance(package.get('data'), list):
            return package['data']
    return None


# 私信记录的 bizType / objectType（370 是会话/预热等明文记录，不需要当消息解析）
MESSAGE_BIZ_TYPE = 40


def is_message_record(entry: object) -> bool:
    """这条记录是不是私信（bizType 或 objectType 命中 40）。

    真实客户端是按 objectType 分派推送记录的，我们两处都看一眼更稳。
    """
    if not isinstance(entry, dict):
        return False
    return entry.get('bizType') == MESSAGE_BIZ_TYPE or entry.get('objectType') == MESSAGE_BIZ_TYPE


def carries_message(message: dict) -> bool:
    """这条推送帧里是否夹带了私信记录。

    用来区分"心跳/ACK 这类本来就没有消息字段的帧"和"该有消息却解析不出来的帧"：
    后者说明报文形状变了，必须报警而不是静默丢弃。
    """
    return any(is_message_record(entry) for entry in _extract_records(message) or [])


def describe_message_records(message: dict) -> str:
    """把帧里疑似私信的记录挑出来做成一行描述，供解析失败时打日志。

    不能直接打印整帧：帧开头永远是 headers（每条推送都一样），截前 300 字符根本到不了
    真正有用的记录，于是每条告警看起来都一模一样。这里只取记录自身的字段与解码结果。
    """
    parts: list[str] = []
    for index, entry in enumerate(_extract_records(message) or []):
        if not is_message_record(entry) or not isinstance(entry, dict):
            continue
        raw = entry.get('data')
        if isinstance(raw, str):
            try:
                detail = f'解码={str(decrypt_data(raw))[:200]}'
            except Exception as e:  # noqa: BLE001 - 这里就是要把失败原因写清楚
                detail = f'data={raw[:200]!r}（解码失败 {type(e).__name__}: {e}）'
        else:
            detail = f'data 非字符串: {type(raw).__name__}'
        parts.append(f'记录{index}(objectType={entry.get("objectType")} bizType={entry.get("bizType")}) {detail}')
    if not parts:
        return '本帧没有 objectType/bizType=40 的记录'
    return f'lwp={message.get("lwp")} 私信记录 {len(parts)} 条: ' + ' | '.join(parts)


def normalize_history_payload(model: dict) -> dict | None:
    """把历史接口的 userMessageModel 归一成与长连接推送一致的 payload 形状。

    两者形状并不一样：

    - 长连接推送：{'1': {'2': cid, '6': {'3': {'5': <内容 JSON>}}, '10': <提醒字段>}}
    - 历史接口（listUserMessages）：cid 在 message.cid；提醒字段（reminderTitle /
      reminderContent / senderUserId）在 message.extension；正文是
      message.content.custom.data（base64 的内容 JSON）。

    老实现直接把 content.custom.data 解出来当 payload，于是 find_message_body
    找不到 ['1']['10']，整条历史消息报 KeyError('message')。

    形状对不上（不是 userMessageModel 或缺 cid）时返回 None，由调用方兜底。
    """
    message = model.get('message')
    if not isinstance(message, dict):
        return None
    cid = message.get('cid')
    if not cid:
        return None

    extension = message.get('extension')
    reminder = dict(extension) if isinstance(extension, dict) else {}
    sender = message.get('sender')
    if isinstance(sender, dict) and not reminder.get('senderUserId'):
        reminder['senderUserId'] = str(sender.get('uid') or '').split('@')[0]
    content = message.get('content')
    custom = content.get('custom') if isinstance(content, dict) else None
    if isinstance(custom, dict) and not reminder.get('reminderContent') and custom.get('summary'):
        reminder['reminderContent'] = custom['summary']
    # 缺字段时补空串：让 extract_message_info 稳定返回，而不是整批历史一起挂掉
    for key in ('senderUserId', 'reminderTitle', 'reminderContent'):
        reminder.setdefault(key, '')

    body: dict = {'2': cid, '10': reminder}
    created = message.get('createAt')
    if created:
        body['5'] = str(created)
    content_json = decode_history_content(message)
    if content_json is not None:
        body['6'] = {'3': {'5': content_json}}
    return {'1': body}


def decode_history_content(message: dict) -> str | None:
    """取历史消息的内容 JSON 字符串（content.custom.data 是它的 base64）。"""
    content = message.get('content')
    custom = content.get('custom') if isinstance(content, dict) else None
    raw = custom.get('data') if isinstance(custom, dict) else None
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return b64decode(raw).decode('utf-8')
    except ValueError, UnicodeDecodeError:
        return None


def iter_payloads(message: dict) -> Iterator[dict]:
    """枚举一条消息里所有可能承载业务数据的 JSON。

    长连接把一批记录放在 body.syncPushPackage.data 里，每条记录各自是
    base64 明文 JSON（会话/预热等）或 base64 MessagePack（私信），
    同一批里混杂多种类型，所以只能逐条解码、逐条判断。
    历史记录接口则是单条 base64 明文 JSON。
    """
    records = _extract_records(message)
    if records is None:
        data = message.get('data')
        records = data if isinstance(data, list) else None
        if records is None and isinstance(data, str):
            records = [data]

    if records is not None:
        for entry in records:
            raw = entry.get('data') if isinstance(entry, dict) else entry
            if not isinstance(raw, str):
                continue
            try:
                yield decrypt_data(raw)
            except Exception as e:  # noqa: BLE001 - 单条解不开就跳过，不影响同批其它记录
                # 记 warning 而不是静默跳过：报文形状变了要能第一时间发现
                logger.warning(f'记录解码失败（{type(e).__name__}: {e}），原文前 60 字符: {raw[:60]!r}')
                continue
        return

    history = message.get('message')
    if isinstance(history, dict):
        # 历史接口的一条 userMessageModel：形状与推送不同，先归一再解析
        normalized = normalize_history_payload(message)
        if normalized is not None:
            yield normalized
            return
        # 兜底：content.custom.data 本身就是推送那种包装 payload
        yield loads(b64decode(history['content']['custom']['data']).decode('utf-8'))


def _walk_dicts(value: Any, depth: int = 0) -> Iterator[dict]:
    if depth > 8:
        return
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from _walk_dicts(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            yield from _walk_dicts(item, depth + 1)


# extract_message_uid 里优先查看的 JSON 字符串字段（普通私信看 bizTag，卡片消息看 extJson）
_UID_JSON_FIELDS = ('bizTag', 'extJson', 'ext')


def extract_message_uid(payload: dict) -> str:
    """取消息的唯一 id。

    闲鱼同一条私信会随多个帧重复下发（实测一条消息出现 6 次），
    必须去重，否则会重复转发到飞书。

    消息 id 可能在三个地方，按可靠性依次找：
    1. 同一层 dict 里的 `messageId` / `msgId`
    2. `bizTag` / `extJson` 这类 JSON 字符串字段
       （普通私信在 bizTag，卡片/系统消息只在 extJson，
       漏了就会重复推送）
    3. 其它任何 JSON 字符串字段（防备报文换字段名）
    """
    for node in _walk_dicts(payload):
        for key in ('messageId', 'msgId'):
            if node.get(key):
                return str(node[key])
        # 已知的 JSON 字符串字段优先，再去扫其它字符串（报文加字段时不至于失配）
        for key in _UID_JSON_FIELDS:
            message_id = _find_message_id_in_json(node.get(key))
            if message_id:
                return message_id
        for value in node.values():
            message_id = _find_message_id_in_json(value)
            if message_id:
                return message_id
    return ''


def _find_message_id_in_json(value: Any) -> str:
    """从 JSON 字符串字段（bizTag / extJson）里抠出 messageId。"""
    if not isinstance(value, str) or not value.startswith('{'):
        return ''
    try:
        parsed = loads(value)
    except JSONDecodeError:
        return ''
    if not isinstance(parsed, dict):
        return ''
    for key in ('messageId', 'msgId'):
        if parsed.get(key):
            return str(parsed[key])
    return ''


def find_message_body(payload: Any) -> dict | None:
    """在 payload 里定位真正承载消息的那一层。

    历史记录：{'1': {'2': cid, '10': {reminder*}}}
    长连接推送：{'1': {'1': {'2': cid, '10': {reminder*}}}}（多包了一层）
    所以不能写死层级，按特征查找。
    """
    if not isinstance(payload, dict):
        return None
    body = payload.get('1')
    if not isinstance(body, dict):
        return None
    if body.get('2') is not None and isinstance(body.get('10'), dict):
        return body
    inner = body.get('1')
    if isinstance(inner, dict) and isinstance(inner.get('10'), dict):
        return inner
    return None


def parse_payload(message: dict) -> dict:
    """取出承载消息的那一层 payload（找不到抛 KeyError）。

    保留此函数是为了向后兼容；新代码建议直接用 extract_message_info()。
    """
    for payload in iter_payloads(message):
        if find_message_body(payload) is not None:
            return payload
    raise KeyError('message')


def extract_message_info(message: dict) -> MessageInfo:
    """把协议消息统一成业务字段；找不到消息字段时抛 KeyError。"""
    for payload in iter_payloads(message):
        body = find_message_body(payload)
        if body is None:
            continue
        reminder = body['10']
        return {
            'cid': str(body['2']).split('@')[0],
            'send_user_id': reminder['senderUserId'],
            'send_user_name': reminder['reminderTitle'],
            'send_message': reminder['reminderContent'],
            'raw': payload,
        }
    raise KeyError('message')


def format_time(value: str | datetime | None) -> str:
    """把 ISO 时间串格式化成界面/卡片用的 MM-DD HH:MM:SS。

    时间统一按带时区的 UTC 存（见各处 datetime.now(UTC)），
    展示时要换回本机时区，否则会比本地时间少几个钟头。
    老配置文件里存的是不带时区的本地时间，原样输出即可。
    """
    if not value:
        return ''
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return value
    if value.tzinfo is not None:
        value = value.astimezone()
    return value.strftime('%m-%d %H:%M:%S')


def extract_message_time(payload: dict) -> str:
    """取消息自身的发生时间（报文里的毫秒时间戳），比"收到的时刻"更准。

    定位消息体必须用 find_message_body：推送里的 payload 比历史记录多包一层
    （payload['1']['1'] vs payload['1']），写死层级会取不到时间。
    取不到就返回空串，由调用方决定要不要展示。
    """
    body = find_message_body(payload)
    if body is None:
        return ''
    created = body.get('5')
    if not created:
        return ''
    try:
        return format_time(datetime.fromtimestamp(int(created) / 1000, tz=UTC))
    except TypeError, ValueError, OSError:
        return ''


def extract_session_title(payload: dict) -> tuple[str, str] | None:
    """从会话/预热记录里取「会话 id → 商品标题」，供消息卡片展示商品。

    这类记录形如 {'chatType': 1, 'sessionId': 60882518754, 'operation': {...,
    'sessionInfo': {'extensions': {'itemTitle': '...'}}}}，标题藏在 extensions 里。
    """
    if not isinstance(payload, dict):
        return None
    session_id = payload.get('sessionId')
    if not session_id:
        session_id = next((node['sessionId'] for node in _walk_dicts(payload) if node.get('sessionId')), None)
    title = next(
        (
            node['itemTitle']
            for node in _walk_dicts(payload)
            if isinstance(node.get('itemTitle'), str) and node['itemTitle']
        ),
        None,
    )
    if not session_id or not title:
        return None
    return str(session_id), title

"""协议工具的 Python 实现测试（原 goofish.js 的功能）。

背景：goofish.js 里那 500 行 `decrypt` 是**被混淆的 MessagePack 解码器**，
已用 msgpack 库取代，并用该文件注释里自带的真实样本逐字段比对过
（样本留在 `tests/data/real_messagepack_samples.txt`，原 js 已删除）。
这里锁住等价性，避免以后被误改。
"""

from __future__ import annotations

from base64 import b64decode, b64encode
from hashlib import md5
from json import dumps, loads
from pathlib import Path
from re import fullmatch

from msgpack import packb
from pytest import mark, raises

from goofishpostman.goofish_utils import (
    carries_message,
    decode_messagepack,
    decrypt,
    decrypt_data,
    describe_message_records,
    extract_message_info,
    extract_message_time,
    extract_message_uid,
    extract_session_title,
    find_message_body,
    generate_device_id,
    generate_mid,
    generate_sign,
    generate_uuid,
)
from goofishpostman.types import MTOP_APP_KEY

from fixtures import (
    AROUSE_PAYLOAD,
    AUDIO_CONTENT,
    IMAGE_CONTENT,
    IMAGE_URL,
    PLATFORM_CARD_CONTENT,
    PUSH_MESSAGE_PAYLOAD,
    SESSION_ID,
    TEXT_CARD_CONTENT,
    TIP_CONTENT,
    TRADE_CARD_CONTENT,
    VIDEO_CONTENT,
    VIDEO_URL,
    encrypted_record,
    plain_record,
    push_frame,
    push_payload_with_content,
)


# ── generate_mid / generate_uuid ─────────────────────────────────────────────
def test_generate_mid_shape() -> None:
    """3 位随机数 + 13 位毫秒时间戳 + ' 0'（与 goofish.js 一致）。"""
    for _ in range(5):
        assert fullmatch(r'\d{1,3}\d{13} 0', generate_mid())


def test_generate_uuid_shape() -> None:
    for _ in range(5):
        assert fullmatch(r'-\d{13}1', generate_uuid())


def test_generate_mid_is_unique_enough() -> None:
    assert len({generate_mid() for _ in range(20)}) > 1


# ── generate_device_id ───────────────────────────────────────────────────────
def test_device_id_is_uuid_v4_shaped_with_user_id() -> None:
    """36 位 UUID v4 形状 + '-' + user_id。"""
    value = generate_device_id('13993122')
    body = value[: -len('-13993122')]
    assert len(body) == 36
    assert body[14] == '4'
    assert [body[i] for i in (8, 13, 18, 23)] == ['-'] * 4
    assert value.endswith('-13993122')
    assert fullmatch(r'[0-9A-Za-z-]{36}', body)


def test_device_id_without_user_id() -> None:
    """扫码登录时还没有 user_id：只留随机部分，不留 'null' 尾巴。"""
    for value in (generate_device_id(), generate_device_id(None)):
        assert value.endswith('-')
        assert 'null' not in value
        assert len(value) == 37


def test_device_id_variant_bit() -> None:
    """第 19 位是 variant 位：JS 用 (3 & nibble) | 8，落在 64 字符表的第 8~15 位。"""
    alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-_'
    for _ in range(50):
        index = alphabet.index(generate_device_id('x')[19])
        assert 8 <= index <= 15, f'variant 索引越界: {index}'


# ── generate_sign ────────────────────────────────────────────────────────────
@mark.parametrize(
    ('timestamp', 'token', 'data'),
    [('1700000000000', 'TOKEN', '{"itemId":"123"}'), ('1789141514667', 'abc', '{}'), ('1', '', 'x')],
)
def test_sign_matches_manual_md5(timestamp: str, token: str, data: str) -> None:
    expected = md5(f'{token}&{timestamp}&{MTOP_APP_KEY}&{data}'.encode()).hexdigest()
    assert generate_sign(timestamp, token, data) == expected


def test_goofish_sign_delegates_to_generate_sign() -> None:
    from goofishpostman.goofish_apis import Goofish

    assert Goofish.sign('1', 'tok', '{}') == generate_sign('1', 'tok', '{}')


# ── MessagePack 解码（原 decrypt） ───────────────────────────────────────────
def test_decode_messagepack_roundtrip() -> None:
    payload = {
        '1': {'2': 'cid@goofish', '10': {'reminderTitle': '买家', 'reminderContent': '在吗', 'senderUserId': '9'}}
    }
    assert decode_messagepack(packb(payload, use_bin_type=True)) == payload


def test_decode_messagepack_accepts_base64_str() -> None:
    payload = {'a': [1, 2, {'b': '中文'}], 'c': True, 'd': None}
    encoded = b64encode(packb(payload, use_bin_type=True)).decode()
    assert decode_messagepack(encoded) == payload


def test_decode_messagepack_keeps_numbers() -> None:
    """JS 的 JSON.stringify 把所有数字转成字符串；Python 保留数字（更准确）。"""
    result = decode_messagepack(packb({'ts': 1742458817389}, use_bin_type=True))
    assert result == {'ts': 1742458817389}
    assert isinstance(result['ts'], int)


def test_decode_messagepack_handles_nested_binary() -> None:
    payload = {'bin': b'\x01\x02\x03'}
    assert decode_messagepack(packb(payload, use_bin_type=True))['bin'] == b'\x01\x02\x03'


def test_decrypt_returns_json_string() -> None:
    """兼容入口：返回 JSON 字符串（原 goofish.js 的返回形式）。"""
    payload = {'1': {'10': {'reminderContent': '你好'}}}
    text = decrypt(b64encode(packb(payload, use_bin_type=True)).decode())
    assert loads(text) == payload
    assert '你好' in text  # 中文不转义


def test_decrypt_data_prefers_plain_json() -> None:
    assert decrypt_data('{"hello":"world"}') == {'hello': 'world'}


def test_decrypt_data_handles_base64_plain_json() -> None:
    """回归：推送里大多数会话/预热记录是 base64 的明文 JSON。

    这条路径曾在改写时漏掉，导致 34 条真实记录全部解码失败（ExtraData）
    并被静默跳过 —— 会话数据全丢。
    """
    payload = {'chatType': 1, 'incrementType': 1, 'sessionId': 59618055770}
    encoded = b64encode(dumps(payload, ensure_ascii=False).encode()).decode()
    assert decrypt_data(encoded) == payload


def test_decrypt_data_falls_back_to_messagepack() -> None:
    payload = {'1': 'x'}
    assert decrypt_data(b64encode(packb(payload, use_bin_type=True)).decode()) == payload


def test_decrypt_data_supports_all_three_shapes() -> None:
    """三种形态都要认：明文 JSON / base64+JSON / base64+MessagePack。"""
    plain = {'a': 1}
    packed = {'b': 2}
    cases = [
        dumps(plain, ensure_ascii=False),
        b64encode(dumps(plain, ensure_ascii=False).encode()).decode(),
        b64encode(packb(packed, use_bin_type=True)).decode(),
    ]
    results = [decrypt_data(case) for case in cases]
    assert results[0] == plain
    assert results[1] == plain
    assert results[2] == packed


def test_decrypt_data_raises_on_garbage() -> None:
    with raises(Exception):  # noqa: B017 - 明文/MessagePack 都失败即非消息体
        decrypt_data('not-json-and-not-messagepack')


def load_real_samples() -> list[str]:
    path = Path(__file__).resolve().parent / 'data' / 'real_messagepack_samples.txt'
    return [line.strip() for line in path.read_text(encoding='utf-8').splitlines() if line.strip() and line[0] != '#']


def test_real_messagepack_samples_still_decode() -> None:
    """用真实报文样本回归（原 goofish.js 注释自带，防止换实现后行为漂移）。"""
    samples = load_real_samples()
    assert samples, '样本文件丢了'

    for payload in samples:
        decoded = decode_messagepack(b64decode(payload))
        assert isinstance(decoded, dict) and decoded


def test_real_messagepack_sample_uses_string_keys() -> None:
    """样本是整数键的 MessagePack，解出来必须变成字符串键。

    JS 版最后过了一遍 `JSON.stringify`，数字键会变成字符串；Python 版如果
    保留整数键，`find_message_body` 按 '1'/'10' 取值就取不到，
    整条消息会被静默丢掉。
    """
    decoded = decode_messagepack(b64decode(load_real_samples()[0]))
    assert decoded['1']['10']['reminderTitle']


def test_real_messagepack_sample_is_a_private_message() -> None:
    """样本要能一路解析成业务字段，而不只是「解出来是个 dict」。"""
    payload = decode_messagepack(b64decode(load_real_samples()[0]))
    body = find_message_body(payload)
    assert body is not None
    reminder = body['10']
    assert body['2'] == '47983389096@goofish'
    assert reminder['senderUserId'] == '3149637063'
    assert reminder['reminderContent'] == '[我已拍下，待付款]'

    # 装进真实推送帧后能解析出同样的业务字段
    info = extract_message_info(push_frame(encrypted_record(payload)))
    assert info['cid'] == '47983389096'
    assert info['send_user_id'] == '3149637063'
    assert info['send_user_name'] == reminder['reminderTitle']
    assert info['send_message'] == '[我已拍下，待付款]'


def test_extract_message_uid_reads_message_id_from_ext_json() -> None:
    """卡片/系统消息的 messageId 只在 extJson 里，bizTag 里没有。

    漏掉的话 extract_message_uid 返回空串 → 去重失效 → 同一条消息推 6 次。
    """
    payload = decode_messagepack(b64decode(load_real_samples()[0]))
    assert loads(payload['1']['10']['bizTag']).get('messageId') is None  # bizTag 里确实没有
    assert extract_message_uid(payload) == 'cc8bc2df7c934df086e0567cb69f1573'


def test_extract_message_time_reads_nested_push_body() -> None:
    """推送里的消息体比历史记录多包一层，时间必须按 find_message_body 定位。

    写成 payload['1'] 时只会命中外层，时间取不到，卡片上就没有时间。
    """
    info = extract_message_info(push_frame(encrypted_record()))
    # 报文里是毫秒时间戳，展示时换成本地时区，格式固定 MM-DD HH:MM:SS
    assert fullmatch(r'\d{2}-\d{2} \d{2}:\d{2}:\d{2}', extract_message_time(info['raw']))


def test_extract_message_time_is_empty_for_non_message_payload() -> None:
    """会话/预热记录里没有消息体，取不到时间应当返回空串而不是抛异常。"""
    assert extract_message_time(AROUSE_PAYLOAD) == ''


def test_extract_session_title_reads_item_title_from_session_info() -> None:
    """会话预热记录里带着「会话 id ↔ 商品标题」，卡片上的商品就是它。

    sessionId 与私信的 cid 同一个 id 空间（实测拿 sessionId 当 cid 能拉到
    历史消息），所以这张表能直接把 cid 映射成商品。
    """
    assert extract_session_title(AROUSE_PAYLOAD) == (
        SESSION_ID,
        '上海gan部在线学习笔记，详情请咨询。标价2026年全年包年',
    )
    assert extract_session_title(PUSH_MESSAGE_PAYLOAD) is None  # 私信里没有标题，不要瞎猜


def test_carries_message_only_for_message_records() -> None:
    """只有带 bizType/objectType=40 记录的帧才算「本该有消息」。"""
    assert carries_message(push_frame(encrypted_record())) is True
    assert carries_message(push_frame(plain_record(AROUSE_PAYLOAD))) is False
    assert carries_message({'lwp': '/!', 'headers': {'mid': 'm'}}) is False


def test_describe_message_records_skips_headers() -> None:
    """告警文本要点出记录自身的字段，而不是帧开头那段永远一样的 headers。

    原来的实现截整帧前 300 字符，打印出来永远是 headers，每条告警长得一样。
    """
    described = describe_message_records(push_frame(encrypted_record()))
    assert '私信记录 1 条' in described
    assert 'objectType=40' in described
    assert '有吗' in described  # 解出来的正文，排查时一眼能看到

    assert describe_message_records(push_frame(plain_record(AROUSE_PAYLOAD))) == '本帧没有 objectType/bizType=40 的记录'


# ── 正文类型（content JSON）──────────────────────────────────────────────────
def text_of(content: dict, reminder: str = '') -> str:
    """走真实链路：把正文包成推送 payload → 解析 → 取可读文案。"""
    from goofishpostman.goofish_live import extract_message_text

    info = extract_message_info(push_frame(encrypted_record(push_payload_with_content(content, reminder))))
    return extract_message_text(info)


def test_text_content_stays_as_is() -> None:
    assert text_of({'contentType': 1, 'text': {'text': '在吗'}}) == '在吗'
    assert text_of({'atUsers': [], 'contentType': 1, 'text': {'text': '  带空格  '}}) == '带空格'


def test_image_content_gives_label_and_url() -> None:
    """图片消息原来只会显示报文里那句 `[图片]`，现在要带上可以点开的地址。"""
    assert text_of(IMAGE_CONTENT) == f'[图片]\n{IMAGE_URL}'


def test_multi_image_content_lists_every_url() -> None:
    two = {'contentType': 2, 'image': {'pics': [{'url': IMAGE_URL}, {'url': 'https://img.alicdn.com/second.jpg'}]}}
    assert text_of(two) == f'[图片]\n{IMAGE_URL}\nhttps://img.alicdn.com/second.jpg'


def test_video_content_gives_url() -> None:
    assert text_of(VIDEO_CONTENT) == f'[视频]\n{VIDEO_URL}'


def test_audio_content_gives_duration_and_url() -> None:
    assert text_of(AUDIO_CONTENT) == '[语音]\n时长 8 秒\nhttps://example.com/voice.amr'


def test_text_card_content_is_stripped_of_html() -> None:
    """卡片里的富文本要变成人能读的文字，内嵌链接转成「文字（url）」。"""
    assert text_of(TEXT_CARD_CONTENT) == (
        '[卡片]\n'
        '物流已签收\n'
        '3天后自动确认收货，如有问题可延长收货 查看详情（fleamarket://order_detail?id=4502273115115018200&role=Buyer）'
    )


def test_tip_content_keeps_text_without_href() -> None:
    """没有 href 的 <a> 只留文字（“叮一下”），不能把标签一起打出来。"""
    assert text_of(TIP_CONTENT) == '[提示]\n想要卖家更快回复？平台帮你催促，点击“叮一下”'


def test_trade_card_content_gives_title_desc_and_button() -> None:
    assert text_of(TRADE_CARD_CONTENT) == (
        '[交易卡片]\n我已修改价格，等待你付款\n请确认价格与协商一致，并在24小时内付款\n去付款：fleamarket://order_detail?id=1&role=buyer'
    )


def test_platform_card_content_gives_subtitle_and_button() -> None:
    assert text_of(PLATFORM_CARD_CONTENT) == (
        '[平台消息]\n快给ta一个评价吧～\n交易体验还满意吗？评价帮更多人选购\n去评价：https://h5.m.goofish.com/wow/moyu/evaluate'
    )


def test_unknown_content_falls_back_to_reminder_text() -> None:
    """认不出的新类型不要瞎标：退回报文自带的那句提醒。"""
    assert text_of({'contentType': 99, 'foo': 'bar'}, reminder='[小程序]') == '[小程序]'
    assert text_of({}) == '[不支持的消息类型]'  # 连提醒都没有时才用最后兜底


def test_platform_tip_is_marked_silent() -> None:
    """平台提示条（14）只记录不推送，其余类型照常推。"""
    from goofishpostman.goofish_utils import extract_message_info as parse
    from goofishpostman.goofish_utils import is_silent_message

    def payload_of(content: dict) -> dict:
        return parse(push_frame(encrypted_record(push_payload_with_content(content))))['raw']

    assert is_silent_message(payload_of(TIP_CONTENT)) is True
    assert is_silent_message(payload_of({'contentType': 1, 'text': {'text': '在吗'}})) is False
    assert is_silent_message(payload_of(IMAGE_CONTENT)) is False
    assert is_silent_message(payload_of(PLATFORM_CARD_CONTENT)) is False
    assert is_silent_message(payload_of({'contentType': 99})) is False  # 认不出的照样推
    assert is_silent_message({'1': {'2': 'x', '10': {}}}) is False  # 没有正文的帧不能炸


def test_trade_cards_are_filtered_by_title() -> None:
    """26 交易卡片只放行"买家拍下/买家付款/收到小红花"，其余文案静音。

    实测某账号 33 条历史里 33 种文案（改价、评价提醒、地址修改、投缘优惠…），
    对卖家有意义的就是这三种。
    """
    from goofishpostman.goofish_utils import extract_message_info as parse
    from goofishpostman.goofish_utils import is_silent_message

    def silent(title: str) -> bool:
        content = {'contentType': 26, 'dxCard': {'item': {'main': {'exContent': {'title': title}}}}}
        payload = parse(push_frame(encrypted_record(push_payload_with_content(content))))['raw']
        return is_silent_message(payload)

    assert silent('我已拍下，待付款') is False
    assert silent('我已付款，等待你发货') is False
    assert silent('收到小红花，心里乐开花！') is False
    for title in (
        '我已修改价格，等待你付款',
        '我完成了评价',
        '记得及时确认收货',
        '我发起了地址修改申请',
        '你还未开通微信收款',
    ):
        assert silent(title) is True, title


def test_message_images_are_extracted_for_upload() -> None:
    """图片消息要能取出图片地址（上传飞书换成 image_key 才能内嵌）。"""
    from goofishpostman.goofish_utils import extract_message_images

    image_payload = extract_message_info(push_frame(encrypted_record(push_payload_with_content(IMAGE_CONTENT))))['raw']
    assert extract_message_images(image_payload) == [IMAGE_URL]

    text_payload = extract_message_info(
        push_frame(encrypted_record(push_payload_with_content({'contentType': 1, 'text': {'text': '在吗'}})))
    )['raw']
    assert extract_message_images(text_payload) == []
    # 视频暂时只给链接：飞书的文件上传接口是另一套（im/v1/files）
    video_payload = extract_message_info(push_frame(encrypted_record(push_payload_with_content(VIDEO_CONTENT))))['raw']
    assert extract_message_images(video_payload) == []


def test_image_content_is_read_from_history_shape_too() -> None:
    """同一份正文，推送与历史记录的包装层不同，两边都要能取到。"""
    from goofishpostman.goofish_utils import describe_message_content, extract_message_content, format_content_text

    history_payload = {
        '1': {
            '2': f'{SESSION_ID}@goofish',
            '6': {'3': {'5': dumps(IMAGE_CONTENT)}},
            '10': {'reminderTitle': '买家', 'reminderContent': '[图片]', 'senderUserId': '2221114099805'},
        }
    }
    parsed = extract_message_content(history_payload)
    assert parsed == IMAGE_CONTENT
    assert format_content_text(describe_message_content(parsed)) == f'[图片]\n{IMAGE_URL}'

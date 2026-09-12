"""私信解析的回归测试。

这里的报文形状全部来自真实抓包（见 fixtures.py）。2026-09 修复的问题：
推送里的私信体在 payload['1']['1']（多包一层），而旧实现只读 payload['1']，
导致所有私信被静默丢弃 —— 网页没有日志、飞书也没有转发。
"""

from __future__ import annotations

from pytest import raises

from goofishpostman.goofish_live import extract_message_text
from goofishpostman.goofish_utils import extract_message_info, extract_message_uid, iter_payloads, parse_payload

from fixtures import (
    AROUSE_PAYLOAD,
    HISTORY_MODEL,
    HISTORY_PAYLOAD,
    MESSAGE_ID,
    PUSH_MESSAGE_PAYLOAD,
    encrypted_record,
    history_record,
    plain_record,
    push_frame,
)


# ── 推送（长连接） ────────────────────────────────────────────────────────────
def test_push_message_is_parsed(stub_decrypt) -> None:
    """核心回归：推送帧里的私信必须被识别出来。"""
    frame = push_frame(encrypted_record())

    info = extract_message_info(frame)

    assert info['send_message'] == '有吗'
    assert info['send_user_name'] == '一站式学习助手'
    assert info['send_user_id'] == '2221114099805'
    assert info['cid'] == '54995284239'  # 去掉 @goofish 后缀
    assert extract_message_text(info) == '有吗'


def test_push_message_id_is_stable(stub_decrypt) -> None:
    """去重要用得上：同一条消息多次下发必须得到同一个 id。"""
    frame = push_frame(encrypted_record(), encrypted_record())
    payloads = list(iter_payloads(frame))
    assert len(payloads) == 2
    assert {extract_message_uid(p) for p in payloads} == {MESSAGE_ID}


def test_arouse_records_are_not_treated_as_messages(stub_decrypt) -> None:
    """会话预热记录混在同一批里，不能被当成私信推给飞书。"""
    frame = push_frame(plain_record(AROUSE_PAYLOAD))
    with raises(KeyError):
        extract_message_info(frame)


def test_message_found_among_mixed_records(stub_decrypt) -> None:
    """真实帧是混合批次：预热 + 私信，必须挑出私信那条。"""
    frame = push_frame(plain_record(AROUSE_PAYLOAD), encrypted_record(), plain_record(AROUSE_PAYLOAD))
    info = extract_message_info(frame)
    assert info['send_message'] == '有吗'


def test_undecodable_records_are_skipped(stub_decrypt) -> None:
    """个别记录解不开时，不能影响同批里其它记录。"""
    broken = {'bizType': 40, 'data': '!!!not-a-valid-payload!!!'}
    frame = push_frame(broken, encrypted_record())
    assert extract_message_info(frame)['send_message'] == '有吗'


def test_non_string_record_data_is_skipped(stub_decrypt) -> None:
    frame = push_frame({'bizType': 40, 'data': None}, encrypted_record())
    assert extract_message_info(frame)['send_message'] == '有吗'


def test_empty_frame_raises(stub_decrypt) -> None:
    with raises(KeyError):
        extract_message_info(push_frame())


# ── 历史记录 ──────────────────────────────────────────────────────────────────
def test_real_history_model_is_parsed() -> None:
    """历史接口的真实 userMessageModel（回归：曾整条报 KeyError('message')）。

    真实形状与推送不同：cid 在 message.cid，提醒字段在 message.extension，
    正文是 content.custom.data（base64 的内容 JSON）。
    """
    info = extract_message_info(HISTORY_MODEL)
    assert info['cid'] == '54995284239'
    assert info['send_user_name'] == '一站式学习助手'
    assert info['send_user_id'] == '2221114099805'
    assert info['send_message'] == '在吗'
    assert extract_message_text(info) == '在吗'


def test_real_history_model_is_deduplicable() -> None:
    """历史消息也要能取到 messageId（bizTag / extJson 里都有）。"""
    info = extract_message_info(HISTORY_MODEL)
    assert extract_message_uid(info['raw']) == MESSAGE_ID


def test_real_history_model_without_extension_still_parses() -> None:
    """没有 extension 的历史条目也不能把整批拉挂（字段补空串）。"""
    model = {'message': {'cid': '1@goofish', 'content': {'custom': {'summary': '在吗'}}}}
    info = extract_message_info(model)
    assert info['cid'] == '1'
    assert info['send_message'] == '在吗'  # 退回 content.custom.summary


def test_history_message_is_parsed() -> None:
    """旧形态（content.custom.data 直接是包装 payload）走兜底路径。"""
    info = extract_message_info(history_record(HISTORY_PAYLOAD))
    assert info['send_message'] == '在吗'
    assert info['send_user_name'] == '买家小王'
    assert info['cid'] == '54995284239'


def test_parse_payload_keeps_working_for_both_shapes(stub_decrypt) -> None:
    """parse_payload 是兼容入口，两种形态都要能取出承载消息的 payload。"""
    assert parse_payload(push_frame(encrypted_record())) == PUSH_MESSAGE_PAYLOAD
    assert parse_payload(history_record(HISTORY_PAYLOAD)) == HISTORY_PAYLOAD


# ── extract_message_text ──────────────────────────────────────────────────────
def test_extract_message_text_falls_back_to_reminder_content() -> None:
    """拿不到内容段时退回 reminderContent。"""
    info = {'raw': {}, 'send_message': '兜底文本', 'send_user_name': 'x'}
    assert extract_message_text(info) == '兜底文本'


def test_extract_message_text_reads_image_placeholder(stub_decrypt) -> None:
    """图片消息给占位符，不是空白。"""
    payload = {
        '1': {
            '1': {
                '2': '1@goofish',
                '10': {'reminderTitle': '买家', 'reminderContent': '[图片]', 'senderUserId': '1'},
                '6': {'3': {'contentType': 3, 'image': {'pics': [{'url': 'http://x/1.png'}]}}},
            }
        }
    }
    info = {'raw': payload, 'send_message': '[图片]', 'send_user_name': '买家'}
    assert extract_message_text(info) == '[图片]'

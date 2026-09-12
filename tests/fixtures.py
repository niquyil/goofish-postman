"""测试夹具：真实抓包得到的闲鱼报文形状（不含任何凭据）。

形状来自 2026-09 的真实账号抓包，改动解析逻辑时以此为准：

- 长连接推送：`body.syncPushPackage.data` 是【列表】，每条记录自带 data 字段
  - `bizType=40` → 密文，需要 `decrypt()`；解密后私信体在 `payload['1']['1']`（多包一层）
  - `bizType=370` → 明文 base64 JSON（会话/预热数据），**没有**私信字段
- 历史记录接口：`message.message.content.custom.data` 是 base64 明文 JSON，
  消息体直接在 `payload['1']`
"""

from __future__ import annotations

from base64 import b64encode
from json import dumps

from msgpack import packb

# 真实报文是 MessagePack：私信记录（bizType=40）解密后就是 packets 里的结构。
# 用真 msgpack 编码，测试里不再需要打桩解密。

# 接收方账号（真实报文里 '4' 字段就是它），用于校验消息归属
RECEIVER_UNB = '13993122'

# 会话 id：私信的 cid 与预热记录的 sessionId 是同一个东西（2026-09 实测 19 个会话，
# 17 个能直接拿 sessionId 当 cid 拉历史消息），卡片上的「商品」正是按它 join 的。
SESSION_ID = '54995284239'

# 推送里的私信（bizType=40 解密后）：聊天内容在 ['6']['3']，发件人信息在 ['10']
PUSH_MESSAGE_PAYLOAD = {
    '1': {
        '1': {
            '1': {'1': '2221114099805@goofish'},
            '2': f'{SESSION_ID}@goofish',
            '3': '4295798983499.PNM',
            '4': f'{RECEIVER_UNB}@goofish',
            '5': '1789142132646',
            '6': {
                '1': 101,
                '3': {
                    '1': '',
                    '2': '有吗',
                    '3': '',
                    '4': 1,
                    '5': '{"atUsers":[],"contentType":1,"text":{"text":"有吗"}}',
                },
            },
            '7': 2,
            '8': 1,
            '9': 0,
            '10': {
                '_appVersion': '7.28.20',
                '_platform': 'android',
                'bizTag': '{"sourceId":"S:1","messageId":"b419afeaa64c4e11aab5fc5a8b12556c"}',
                'detailNotice': '有吗',
                'extJson': '{"messageId":"b419afeaa64c4e11aab5fc5a8b12556c","tag":"u"}',
                'reminderContent': '有吗',
                'reminderNotice': '发来一条新消息',
                'reminderTitle': '一站式学习助手',
                'senderUserId': '2221114099805',
                'senderUserType': '0',
                'sessionType': '1',
            },
            '12': 1,
        },
        '3': {'needPush': 'true'},
    }
}

MESSAGE_ID = 'b419afeaa64c4e11aab5fc5a8b12556c'

# 会话预热记录（bizType=370，明文 base64）：没有私信字段，必须被忽略；
# 但里面带着「会话 id ↔ 商品标题」，卡片上的「商品」字段就靠它。
# 字段照抄 2026-09 真实抓包（operation.sessionInfo.extensions）。
AROUSE_PAYLOAD = {
    'chatType': 1,
    'incrementType': 1,
    'operation': {
        'content': {
            'contentType': 8,
            'sessionArouse': {'sessionArouseInfo': {'chatScripInfo': [{'chatScrip': '是全新的吗？'}]}},
        },
        'receiverIds': [RECEIVER_UNB],
        'sessionInfo': {
            'createTime': 1774454601000,
            'extensions': {
                'itemId': '1033564108655',
                'itemTitle': '上海gan部在线学习笔记，详情请咨询。标价2026年全年包年',
                'itemSellerId': '2221114099805',
                'ownerUserId': '2221114099805',
                'extUserId': RECEIVER_UNB,
            },
            'groupOwnerId': '2221114099805',
            'sessionId': SESSION_ID,
            'sessionType': 1,
            'type': 1,
        },
    },
    'sessionId': SESSION_ID,
}

# 历史记录接口的消息体：消息直接在 payload['1']
HISTORY_PAYLOAD = {
    '1': {
        '2': '54995284239@goofish',
        '10': {'reminderTitle': '买家小王', 'reminderContent': '在吗', 'senderUserId': '2221114099805'},
    }
}


def encode(payload: dict) -> str:
    """编成记录里的 data 字段（base64 明文 JSON，会话/预热类记录用）。"""
    return b64encode(dumps(payload, ensure_ascii=False).encode('utf-8')).decode('utf-8')


def encode_messagepack(payload: dict) -> str:
    """编成真实私信记录的 data 字段（base64 + MessagePack，与线上一致）。"""
    return b64encode(packb(payload, use_bin_type=True)).decode('utf-8')


def push_frame(*records: dict) -> dict:
    """组装一个长连接推送帧。"""
    return {'lwp': '/s/sync', 'headers': {'mid': 'm1'}, 'body': {'syncPushPackage': {'data': list(records)}}}


def encrypted_record(payload: dict | None = None) -> dict:
    """bizType=40 的私信记录（base64 + MessagePack）。

    `payload` 可用于构造异常报文；默认用 PUSH_MESSAGE_PAYLOAD。
    """
    return {
        'bizType': 40,
        'objectType': 40,
        'streamId': '40',
        'data': encode_messagepack(payload if payload is not None else PUSH_MESSAGE_PAYLOAD),
    }


def plain_record(payload: dict) -> dict:
    """bizType=370 的明文记录。"""
    return {'bizType': 370, 'objectType': 370000, 'streamId': '370', 'data': encode(payload)}


def history_record(payload: dict) -> dict:
    """历史记录接口返回的一条 userMessageModel（旧实现假设的形态）。

    真实形态见 HISTORY_MODEL：content.custom.data 是【内容 JSON】的 base64，
    而不是推送那种包装 payload。这里保留这个形态用于兜底路径的测试。
    """
    return {'message': {'content': {'custom': {'data': encode(payload)}}}}


# 历史接口（listUserMessages）真实返回的一条 userMessageModel（2026-09 抓包，
# 字段与层级照抄，只把用户 id / 昵称等换成仓库里已有的测试值）。
HISTORY_MODEL = {
    'readStatus': 2,
    'userExtension': {'needPush': 'false'},
    'recallFeature': {'showRecallStatusSetting': 1, 'extension': {}, 'code': '', 'operatorType': 0, 'operatorUid': ''},
    'message': {
        'extension': {
            'reminderContent': '在吗',
            'reminderTitle': '一站式学习助手',
            'senderUserId': '2221114099805',
            'senderUserType': '0',
            'sessionType': '1',
            'bizTag': f'{{"sourceId":"S:1","messageId":"{MESSAGE_ID}"}}',
            'extJson': f'{{"messageId":"{MESSAGE_ID}","tag":"u"}}',
            'reminderUrl': f'fleamarket://message_chat?itemId=1&peerUserId=2221114099805&sid={SESSION_ID}&adv=no',
        },
        'messageId': '4026576314661.PNM',
        'createAt': 1774619291105,
        'content': {
            'custom': {
                'summary': '在吗',
                'data': b64encode(dumps({'contentType': 1, 'text': {'text': '在吗'}}).encode()).decode(),
                'title': '',
                'type': 1,
                'degrade': '',
            },
            'contentType': 101,
        },
        'sender': {'uid': '2221114099805@goofish', 'tag': 0},
        'receivers': [],
        'receiverCount': 2,
        'cid': f'{SESSION_ID}@goofish',
    },
    'msgStatus': 1,
}

# ── 各类型正文（content JSON）────────────────────────────────────────────────
# 形状照抄 2026-09 真实抓包（同一个账号 19 个会话、248 条历史里出现过的全部类型），
# 只有长链接做了保留原样的处理：它们本来就是公开的图片/视频地址。
IMAGE_URL = 'https://img.alicdn.com/imgextra/i4/3123133723/O1CN01RSAPMB1dNBgdIEGcB_!!3123133723-53-xy_chat.heic'
VIDEO_URL = 'http://wantu-xm4-xianyu-video-hz.oss-cn-hangzhou.aliyuncs.com/aus/xianyu_imvod_private/1700813367/5c13a4e.mp4?video_id=560521825334'

# contentType=2 图片：pics 可以有多张
IMAGE_CONTENT = {
    'atUsers': [],
    'contentType': 2,
    'image': {'pics': [{'height': 1440, 'type': 0, 'url': IMAGE_URL, 'width': 1080}]},
}

# contentType=3 语音：形状来自 types.AudioMessage（真实会话里暂时没出现过）
AUDIO_CONTENT = {'contentType': 3, 'audio': {'url': 'https://example.com/voice.amr', 'duration': 8}}

# contentType=4 视频：url 是播放地址，snapshot 是封面图
VIDEO_CONTENT = {
    'atUsers': [],
    'contentType': 4,
    'video': {'duration': 0, 'height': 1440, 'snapshot': f'{VIDEO_URL}.jpg', 'url': VIDEO_URL, 'width': 1080},
}

# contentType=6 文本卡片：title/content 里是带 HTML 的富文本
TEXT_CARD_CONTENT = {
    'contentType': 6,
    'textCard': {
        'title': '<strong><font color="#000000" fontFamily="xianyubeta">物流已签收</font></strong>',
        'content': (
            '<strong><font color="#999999">3天后自动确认收货，如有问题可延长收货 '
            '<a size=13 href="fleamarket://order_detail?id=4502273115115018200&role=Buyer" target="_blank">'
            '查看详情</a></font></strong>'
        ),
    },
}

# contentType=14 提示条：纯平台提示（没有 href 的 <a> 只留文字）
TIP_CONTENT = {
    'contentType': 14,
    'tip': {
        'argInfo': {'arg1': 'Button-MsgTips', 'args': {'sessionId': SESSION_ID}},
        'tip': ' 想要卖家更快回复？平台帮你催促，点击<a size=13 data-intent=\'{"intentType":1}\'>“叮一下”</a>',
    },
}

# contentType=26 交易卡片：标题 + 说明 + 按钮
TRADE_CARD_CONTENT = {
    'contentType': 26,
    'dxCard': {
        'item': {
            'main': {
                'exContent': {
                    'desc': '请确认价格与协商一致，并在24小时内付款',
                    'title': '我已修改价格，等待你付款',
                    'button': {'text': '去付款', 'targetUrl': 'fleamarket://order_detail?id=1&role=buyer'},
                },
                'targetUrl': '',
            }
        },
        'template': {'name': 'idlefish_message_trade_chat_card'},
    },
}

# contentType=25 平台消息卡：只有 subTitle，没有 desc
PLATFORM_CARD_CONTENT = {
    'contentType': 25,
    'dxCard': {
        'item': {
            'main': {
                'exContent': {
                    'subTitle': '交易体验还满意吗？评价帮更多人选购',
                    'title': '快给ta一个评价吧～',
                    'button': {'text': '去评价', 'targetUrl': 'https://h5.m.goofish.com/wow/moyu/evaluate'},
                },
                'targetUrl': '',
            }
        }
    },
}

# 交易卡片里会被放行的文案之一（KEPT_TRADE_CARD_TITLES），其余文案会被静音
KEPT_TRADE_CARD_CONTENT = {
    'contentType': 26,
    'dxCard': {
        'item': {
            'main': {
                'exContent': {
                    'desc': '请包装好商品，并按我在闲鱼上提供的地址发货',
                    'title': '我已付款，等待你发货',
                    'button': {
                        'text': '查看详情',
                        'targetUrl': 'fleamarket://order_detail?id=5118251546854009018&role=Buyer',
                    },
                },
                'targetUrl': '',
            }
        }
    },
}


def push_payload_with_content(content: dict, reminder_content: str = '', message_id: str = MESSAGE_ID) -> dict:
    """把一条正文 JSON 包成真实推送形态的 payload（比历史记录多一层 ['1']['1']）。

    `message_id` 是去重用的 id：同一条消息重复下发时它保持不变，所以要造多条不同
    消息的用例必须显式传不同的值，否则后一条会被当成重复而不转发。
    """
    return {
        '1': {
            '1': {
                '1': {'1': '2221114099805@goofish'},
                '2': f'{SESSION_ID}@goofish',
                '3': '4295798983499.PNM',
                '4': f'{RECEIVER_UNB}@goofish',
                '5': '1789142132646',
                '6': {'1': 101, '3': {'1': '', '2': reminder_content, '3': '', '4': 1, '5': dumps(content)}},
                '7': 2,
                '8': 1,
                '9': 0,
                '10': {
                    'bizTag': f'{{"sourceId":"S:1","messageId":"{message_id}"}}',
                    'extJson': f'{{"messageId":"{message_id}","tag":"u"}}',
                    'reminderContent': reminder_content,
                    'reminderTitle': '一站式学习助手',
                    'senderUserId': '2221114099805',
                    'senderUserType': '0',
                    'sessionType': '1',
                },
                '12': 1,
            },
            '3': {'needPush': 'true'},
        }
    }

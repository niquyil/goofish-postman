import asyncio
import time
from base64 import b64decode, b64encode
from json import dumps, loads
from threading import Thread

from loguru import logger
from websockets import ClientConnection, connect

from .cookies import Cookies
from .goofish_apis import Goofish
from .goofish_utils import decrypt, generate_device_id, generate_mid, generate_uuid
from .headers import USER_AGENT
from .types import Message, TextMessage


class GoofishLive:
    base_url = 'wss://wss-goofish.dingtalk.com/'

    def __init__(self, cookies_str: str) -> None:
        self.cookies_str = cookies_str
        self.cookies = Cookies.from_str(cookies_str)
        self.myid = self.cookies['unb']
        self.device_id = generate_device_id(self.myid)
        self.goofish = Goofish(cookies=self.cookies, device_id=self.device_id)

    async def list_all_conversations(self, cid):
        headers = {
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Cache-Control': 'no-cache',
            'Connection': 'Upgrade',
            'Cookie': self.goofish.cookies,
            'Host': 'wss-goofish.dingtalk.com',
            'Origin': 'https://www.goofish.com',
            'Pragma': 'no-cache',
            'User-Agent': USER_AGENT,
        }
        async with connect(uri=self.base_url, additional_headers=headers) as websocket:
            asyncio.create_task(self.init(websocket))
            send_mid = generate_mid()
            msg = {
                'lwp': '/r/MessageManager/listUserMessages',
                'headers': {'mid': send_mid},
                'body': [f'{cid}@goofish', False, 9007199254740991, 20, False],
            }
            user_message_models = []
            async for message in websocket:
                try:
                    message = loads(message)
                    ack = {
                        'code': 200,
                        'headers': {
                            'mid': message['headers'].get('mid') or generate_mid(),
                            'sid': message['headers'].get('sid') or '',
                        },
                    }
                    if 'app-key' in message['headers']:
                        ack['headers']['app-key'] = message['headers']['app-key']
                    if 'ua' in message['headers']:
                        ack['headers']['ua'] = message['headers']['ua']
                    if 'dt' in message['headers']:
                        ack['headers']['dt'] = message['headers']['dt']
                    await websocket.send(dumps(ack))
                except Exception as e:
                    logger.error(e)
                try:
                    if 'lwp' in message and message['lwp'] == '/s/vulcan':
                        await websocket.send(dumps(msg))
                    recv_mid = message['headers'].get('mid') or ''
                    if recv_mid == send_mid:
                        logger.info(f'user history message: {message}')
                        has_more = message['body']['hasMore'] == 1
                        next_cursor = message['body']['nextCursor']
                        for user_message in message['body']['userMessageModels']:
                            send_user_name = user_message['message']['extension']['reminderTitle']
                            send_user_id = user_message['message']['extension']['senderUserId']
                            send_message_base64 = user_message['message']['content']['custom']['data']
                            send_message_json = loads(b64decode(send_message_base64).decode('utf-8'))
                            user_message_models.insert(
                                0,
                                {
                                    'send_user_id': send_user_id,
                                    'send_user_name': send_user_name,
                                    'message': send_message_json,
                                },
                            )
                        if has_more:
                            logger.info(f'has more history messages, next cursor: {next_cursor}')
                            send_mid = generate_mid()
                            msg['headers']['mid'] = send_mid
                            msg['body'][2] = next_cursor
                            await websocket.send(dumps(msg))
                        else:
                            return user_message_models
                except Exception as e:
                    logger.error(e)
                    return user_message_models

    async def create_chat(self, websocket: ClientConnection, toid: str, item_id: str='891198795482'):
        msg = {
            'lwp': '/r/SingleChatConversation/create',
            'headers': {'mid': generate_mid()},
            'body': [
                {
                    'pairFirst': f'{toid}@goofish',
                    'pairSecond': f'{self.myid}@goofish',
                    'bizType': '1',
                    'extension': {'itemId': item_id},
                    'ctx': {'appVersion': '1.0', 'platform': 'web'},
                }
            ],
        }
        await websocket.send(dumps(msg))

    async def send_message(self, websocket: ClientConnection, cid: str, toid: str, message: Message):
        msg = {
            'lwp': '/r/MessageSend/sendByReceiverScope',
            'headers': {'mid': generate_mid()},
            'body': [
                {
                    'uuid': generate_uuid(),
                    'cid': f'{cid}@goofish',
                    'conversationType': 1,
                    'content': {'contentType': 101, 'custom': {'type': None, 'data': None}},
                    'redPointPolicy': 0,
                    'extension': {'extJson': '{}'},
                    'ctx': {'appVersion': '1.0', 'platform': 'web'},
                    'mtags': {},
                    'msgReadStatusSetting': 1,
                },
                {'actualReceivers': [f'{toid}@goofish', f'{self.myid}@goofish']},
            ],
        }
        match message.type:
            case 'text':
                payload = {'contentType': 1, 'text': {'text': message.text}}
                text_base64 = str(b64encode(dumps(payload).encode('utf-8')), 'utf-8')
                msg['body'][0]['content']['custom']['type'] = 1
                msg['body'][0]['content']['custom']['data'] = text_base64
            case 'image':
                payload = {
                    'contentType': 2,
                    'image': {
                        'pics': [
                            {'type': 0, 'url': message.url, 'width': message.width, 'height': message.height}
                        ]
                    },
                }
                image_base64 = str(b64encode(dumps(payload).encode('utf-8')), 'utf-8')
                msg['body'][0]['content']['custom']['type'] = 2
                msg['body'][0]['content']['custom']['data'] = image_base64
            case 'audio':
                # TODO: handle audio message
                logger.error(f'不支持的消息类型: {message.type}')
                return
        await websocket.send(dumps(msg))

    async def init(self, websocket: ClientConnection):
        token = self.goofish.get_token().get('data', {}).get('accessToken', '')
        # token = data['data']['accessToken'] if 'data' in data and 'accessToken' in data['data'] else ''
        if not token:
            logger.error('获取token失败')
            exit(0)
        msg = {
            'lwp': '/reg',
            'headers': {
                'cache-header': 'app-key token ua wv',
                'app-key': '444e9908a51d1cb236a27862abc769c9',
                'token': token,
                'ua': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5',
                'dt': 'j',
                'wv': 'im:3,au:3,sy:6',
                'sync': '0,0;0;0;',
                'did': self.device_id,
                'mid': generate_mid(),
            },
        }
        await websocket.send(dumps(msg))
        current_time = int(time.time() * 1000)
        msg = {
            'lwp': '/r/SyncStatus/ackDiff',
            'headers': {'mid': generate_mid()},
            'body': [
                {
                    'pipeline': 'sync',
                    'tooLong2Tag': 'PNM,1',
                    'channel': 'sync',
                    'topic': 'sync',
                    'highPts': 0,
                    'pts': current_time * 1000,
                    'seq': 0,
                    'timestamp': current_time,
                }
            ],
        }
        await websocket.send(dumps(msg))
        logger.info('init')

    @staticmethod
    async def heart_beat(websocket: ClientConnection) -> None:
        while True:
            msg = {'lwp': '/!', 'headers': {'mid': generate_mid()}}
            await websocket.send(dumps(msg))
            await asyncio.sleep(15)

    def user_alive(self) -> None:
        while True:
            time.sleep(600)
            self.goofish.refresh_token()

    async def main(self) -> None:
        headers = {
            'Accept-Encoding': 'gzip, deflate, br, zstd',
            'Accept-Language': 'zh-CN,zh;q=0.9',
            'Cache-Control': 'no-cache',
            'Connection': 'Upgrade',
            'Cookie': self.goofish.cookies,
            'Host': 'wss-goofish.dingtalk.com',
            'Origin': 'https://www.goofish.com',
            'Pragma': 'no-cache',
            'User-Agent': USER_AGENT,
        }
        Thread(target=self.user_alive).start()
        async with connect(self.base_url, additional_headers=headers) as websocket:
            asyncio.create_task(self.init(websocket))
            asyncio.create_task(self.heart_beat(websocket))
            async for message in websocket:
                # logger.info(f"message: {message}")
                message = loads(message)
                ack = {
                    'code': 200,
                    'headers': {
                        'mid': message['headers'].get('mid') or generate_mid(),
                        'sid': message['headers'].get('sid') or '',
                    },
                }
                if 'app-key' in message['headers']:
                    ack['headers']['app-key'] = message['headers']['app-key']
                if 'ua' in message['headers']:
                    ack['headers']['ua'] = message['headers']['ua']
                if 'dt' in message['headers']:
                    ack['headers']['dt'] = message['headers']['dt']
                await websocket.send(dumps(ack))

                await self.handle_message(message, websocket)

    async def handle_message(self, message, websocket: ClientConnection) -> None:
        try:
            data = message['body']['syncPushPackage']['data'][0]['data']
            data = loads(data)
            # logger.info(f"无需解密 message: {data}")
        except Exception as e:
            try:
                data = decrypt(data)
                message = loads(data)
                # logger.info(f"解密的 message: {message}")

                send_user_name = message['1']['10']['reminderTitle']
                send_user_id = message['1']['10']['senderUserId']
                send_message = message['1']['10']['reminderContent']
                logger.info(f'user: {send_user_name}, 发送给我的信息 message: {send_message}')

                cid = message['1']['2']
                cid = cid.split('@')[0]

                # 回复文字
                # reply = f'Hello, {send_user_name}! I am a robot. I am not available now. I will reply to you later.'
                reply = f'{send_user_name} 说了: {send_message}'
                await self.send_message(websocket, cid, send_user_id, TextMessage(text=reply))

                # 回复图片
                # res_json = self.xianyu.upload_media(r"D:\Desktop\1.png")
                # image_object = res_json["object"]
                # width, height = map(int, image_object["pix"].split('x'))
                # await self.send_msg(websocket, cid, send_user_id, make_image(image_object["url"], width, height))
            except Exception as e:
                logger.error(e)

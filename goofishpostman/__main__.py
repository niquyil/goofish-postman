from __future__ import annotations

from argparse import ArgumentParser
from asyncio import run
from os import getenv
from pathlib import Path
from sys import argv as sys_argv  # 本文件的 main() 有同名参数 argv，导入时改名
from typing import TYPE_CHECKING

from dotenv import load_dotenv
from loguru import logger

from .accounts import FeishuNotifier
from .goofish_live import GoofishLive, extract_message_text
from .goofish_utils import extract_message_images, extract_message_time
from .path import ENV_FILE
from .store import Store
from .supervisor import Supervisor

if TYPE_CHECKING:
    from websockets import ClientConnection

    from .types import MessageInfo


class GoofishPostman(GoofishLive):
    """单账号模式：把收到的私信转发到飞书（读取 .env 里的应用凭据）。"""

    def __init__(self, cookies_str: str, notifier: FeishuNotifier) -> None:
        super().__init__(cookies_str)
        self.notifier = notifier

    async def handle_message(self, message: MessageInfo, websocket: ClientConnection) -> None:
        details = {}
        created = extract_message_time(message['raw'])
        if created:
            details['时间'] = created
        await self.notifier.send_card(
            title=f'{message["send_user_name"]} → {self.username}',
            content=extract_message_text(message),
            details=details,
            images=extract_message_images(message['raw']),
        )


def run_single() -> None:
    load_dotenv(ENV_FILE)
    notifier = FeishuNotifier(
        app_id=getenv('APP_ID', ''), app_secret=getenv('APP_SECRET', ''), chat_id=getenv('CHAT_ID', '')
    )
    run(GoofishPostman(cookies_str=getenv('COOKIE_STR'), notifier=notifier).main())


async def run_web(store: Store, host: str = '', port: int = 0) -> None:
    """多账号模式：起 Web 管理界面，按配置拉起所有启用的账号。"""
    from .web import serve

    notify = store.data.notify
    supervisor = Supervisor(
        store, FeishuNotifier(app_id=notify.app_id, app_secret=notify.app_secret, chat_id=notify.chat_id)
    )
    await supervisor.start_enabled()
    await serve(store, supervisor, host=host, port=port)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog='goofishpostman', description='闲鱼消息转发 / 多账号汇总管理台')
    sub = parser.add_subparsers(dest='command')

    sub.add_parser('run', help='单账号模式：按 .env 里的 COOKIE_STR 监听并转发飞书')

    web = sub.add_parser('web', help='多账号模式：启动 Web 管理界面')
    web.add_argument('--host', default='', help='监听地址（默认取配置文件）')
    web.add_argument('--port', type=int, default=0, help='监听端口（默认取配置文件）')
    web.add_argument('--data', type=Path, default=None, help='账号配置文件路径（默认在用户目录）')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # 不带子命令时保持老入口的行为：单账号模式
    args = parser.parse_args(argv if argv is not None else (sys_argv[1:] or ['run']))
    logger.remove()
    logger.add(lambda message: print(message, end=''), level='INFO')

    if args.command == 'web':
        store = Store(args.data)
        web_settings = store.data.web
        host, port = args.host or web_settings.host, args.port or web_settings.port
        logger.info(f'配置文件: {store.path}')
        if store.data.notify.configured:
            logger.info(f'飞书推送已启用（应用 {store.data.notify.app_id} → 群 {store.data.notify.chat_id}）')
        else:
            logger.warning('尚未配置飞书推送（应用凭据或目标群缺失），消息只会在界面里显示')
        try:
            run(run_web(store, host=host, port=port))
        except KeyboardInterrupt:
            logger.info('已退出')
        return 0

    run_single()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

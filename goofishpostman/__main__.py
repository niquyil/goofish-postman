from __future__ import annotations

from argparse import ArgumentParser
from asyncio import Future, create_task, get_running_loop, run
from pathlib import Path
from sys import argv as sys_argv  # 本文件的 main() 有同名参数 argv，导入时改名
from webbrowser import open as open_browser

from anyio import to_thread
from loguru import logger

from .accounts import FeishuNotifier, load_sdk
from .store import Store
from .supervisor import Supervisor


async def warm_up_feishu_sdk() -> None:
    """后台预热飞书 SDK。

    `import lark_oapi` 实测要 9~10 秒（它把全部 API 的事件处理器都导了一遍），
    所以在后台线程里先导好，避免落在启动路径上、也避免第一次转发时才发现要等。
    """
    await to_thread.run_sync(load_sdk)
    logger.debug('飞书 SDK 预热完成')


async def open_console(ready: Future) -> None:
    """等管理界面真正监听后，用系统默认浏览器把它打开。"""
    url = await ready
    try:
        # webbrowser.open 是阻塞调用，丢线程里；无图形环境时它会返回 False
        if not await to_thread.run_sync(open_browser, url):
            logger.warning(f'没能自动打开浏览器，请手动访问 {url}')
    except Exception as e:  # noqa: BLE001 - 打不开浏览器不该影响服务
        logger.warning(f'自动打开浏览器失败（{type(e).__name__}: {e}），请手动访问 {url}')


async def run_web(store: Store, host: str = '', port: int = 0, browser: bool = True) -> None:
    """起 Web 管理界面，并按配置拉起所有启用的账号。"""
    from .feishu_events import FeishuReplyListener
    from .web import serve

    notify = store.data.notify
    supervisor = Supervisor(
        store, FeishuNotifier(app_id=notify.app_id, app_secret=notify.app_secret, chat_id=notify.chat_id)
    )
    if notify.has_app_credentials:
        create_task(warm_up_feishu_sdk())
        # 飞书里回复卡片 → 闲鱼私信（长连接收事件，不需要公网地址）
        FeishuReplyListener(
            app_id=notify.app_id,
            app_secret=notify.app_secret,
            on_reply=supervisor.forward_feishu_reply,
            on_ignored=supervisor.note_ignored_feishu_event,
        ).start()
    await supervisor.start_enabled()

    ready: Future = get_running_loop().create_future()
    if browser:
        create_task(open_console(ready))
    await serve(store, supervisor, host=host, port=port, ready=ready)


def build_parser() -> ArgumentParser:
    parser = ArgumentParser(prog='goofishpostman', description='闲鱼私信汇总管理台')
    parser.add_argument('--host', default='', help='监听地址（默认取配置文件）')
    parser.add_argument('--port', type=int, default=0, help='监听端口（默认取配置文件）')
    parser.add_argument('--data', type=Path, default=None, help='账号配置文件路径（默认在用户目录）')
    parser.add_argument('--no-browser', action='store_true', help='启动后不自动打开浏览器')
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = list(argv if argv is not None else sys_argv[1:])
    # 兼容旧写法 `goofishpostman web --port 9000`：现在只有 Web 一种模式，子命令可以省略
    if arguments and arguments[0] == 'web':
        arguments = arguments[1:]
    elif arguments and arguments[0] == 'run':
        print('单账号命令行模式已移除，直接运行 `python -m goofishpostman` 打开 Web 管理台即可')
        return 2

    args = build_parser().parse_args(arguments)
    logger.remove()
    logger.add(lambda message: print(message, end=''), level='INFO')

    store = Store(args.data)
    web_settings = store.data.web
    host, port = args.host or web_settings.host, args.port or web_settings.port
    logger.info(f'配置文件: {store.path}')
    if store.data.notify.configured:
        logger.info(f'飞书推送已启用（应用 {store.data.notify.app_id} → 群 {store.data.notify.chat_id}）')
    else:
        logger.warning('尚未配置飞书推送（应用凭据或目标群缺失），消息只会在界面里显示')
    try:
        run(run_web(store, host=host, port=port, browser=not args.no_browser))
    except KeyboardInterrupt:
        logger.info('已退出')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

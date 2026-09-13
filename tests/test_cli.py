"""命令行入口的参数解析与启动行为（单账号模式已移除）。"""

from __future__ import annotations

from asyncio import create_task, get_running_loop, run

from goofishpostman.__main__ import build_parser, main, open_console
from goofishpostman.web import console_url


def test_parser_defaults_to_config_file_settings() -> None:
    args = build_parser().parse_args([])
    assert (args.host, args.port, args.data) == ('', 0, None)
    assert args.no_browser is False  # 默认启动后自动打开浏览器


def test_parser_reads_explicit_overrides() -> None:
    args = build_parser().parse_args(['--host', '0.0.0.0', '--port', '9000', '--no-browser'])
    assert args.host == '0.0.0.0'
    assert args.port == 9000
    assert args.no_browser is True


def test_removed_run_subcommand_says_so(capsys) -> None:
    """旧文档里的 `run`（单账号命令行模式）已经移除，要给一句人话而不是 argparse 报错。"""
    assert main(['run']) == 2
    assert '已移除' in capsys.readouterr().out


def test_console_url_uses_loopback_for_wildcard_hosts() -> None:
    """监听 0.0.0.0 时浏览器打不开，要换成回环地址。"""
    assert console_url(host='0.0.0.0', port=8848) == 'http://127.0.0.1:8848'
    assert console_url(host='', port=8848) == 'http://127.0.0.1:8848'
    assert console_url(host='192.168.1.5', port=8080) == 'http://192.168.1.5:8080'


def test_console_is_opened_once_the_server_is_ready(monkeypatch) -> None:
    """管理界面监听成功后自动打开浏览器（地址就是 serve 回报的那个）。"""
    opened: list[str] = []
    monkeypatch.setattr(target='goofishpostman.__main__.open_browser', name=lambda url: opened.append(url) or True)

    async def flow() -> None:
        ready = get_running_loop().create_future()
        task = create_task(open_console(ready))
        ready.set_result('http://127.0.0.1:8848')
        await task

    run(flow())
    assert opened == ['http://127.0.0.1:8848']

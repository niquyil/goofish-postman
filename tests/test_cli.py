"""命令行入口的参数解析（单账号模式已移除）。"""

from __future__ import annotations

from goofishpostman.__main__ import build_parser, main


def test_parser_defaults_to_config_file_settings() -> None:
    args = build_parser().parse_args([])
    assert (args.host, args.port, args.data) == ('', 0, None)


def test_parser_reads_explicit_overrides() -> None:
    args = build_parser().parse_args(['--host', '0.0.0.0', '--port', '9000'])
    assert args.host == '0.0.0.0'
    assert args.port == 9000


def test_removed_run_subcommand_says_so(capsys) -> None:
    """旧文档里的 `run`（单账号命令行模式）已经移除，要给一句人话而不是 argparse 报错。"""
    assert main(['run']) == 2
    assert '已移除' in capsys.readouterr().out

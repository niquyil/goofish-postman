"""测试公共夹具。

不使用 pytest 的 tmp_path：某些受限环境对系统临时目录只有读权限，
这里把测试目录放在仓库内的 .tests-tmp/ 下，结束时统一清理。
"""

from __future__ import annotations

from collections.abc import Iterator
from itertools import count
from pathlib import Path
from shutil import rmtree

from pytest import fixture

_ROOT = Path(__file__).resolve().parent.parent
_BASE = _ROOT / '.tests-tmp'
_counter = count()

# 抓包里的密文记录用这个占位符表示，真实场景下是二进制密文的 base64
ENCRYPTED_PLACEHOLDER = 'ENCRYPTED-BLOB'


@fixture(scope='session', autouse=True)
def _clean_base() -> Iterator[None]:
    rmtree(_BASE, ignore_errors=True)
    _BASE.mkdir(parents=True, exist_ok=True)
    try:
        yield
    finally:
        rmtree(_BASE, ignore_errors=True)


@fixture
def tmp_dir() -> Path:
    path = _BASE / f'case-{next(_counter)}'
    path.mkdir(parents=True, exist_ok=True)
    return path


@fixture
def stub_decrypt(monkeypatch):
    """已废弃：私信现在是真实 MessagePack（见 fixtures.encode_messagepack），
    不再需要打桩解密。保留此夹具只是让旧测试不必改动。
    """
    return {}

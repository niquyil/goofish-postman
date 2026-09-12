from __future__ import annotations

from os import name
from pathlib import Path

match name:
    case 'nt':
        USER_DIR = Path(r'~\AppData\Roaming\GoofishPostman').expanduser()
    case 'posix':
        USER_DIR = Path(r'~\.goofish-postman').expanduser()
    case _:
        raise NotImplementedError

ENV_FILE = USER_DIR / '.env'
DATA_FILE = USER_DIR / 'accounts.json'
STATIC_DIR = Path(__file__).resolve().parent / 'webui'
TEMPLATE_DIR = STATIC_DIR / 'templates'

# 构建标记：改动前端/接口后手动递增。除了打进启动日志，
# 模板还会用它给静态资源拼查询串（/static/app.js?v=...），所以必须递增才能让浏览器换新文件
BUILD_STAMP = '2026-09-13-app-transport'

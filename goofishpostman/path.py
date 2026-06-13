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

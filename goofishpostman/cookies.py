from __future__ import annotations

from typing import TYPE_CHECKING

from requests.cookies import RequestsCookieJar

if TYPE_CHECKING:
    from requests import Session


def drop_cookies(jar: RequestsCookieJar, *names: str) -> list[str]:
    """按名字删掉 jar 里的 cookie，返回真正删掉的名字。

    `RequestsCookieJar.clear(name=...)` 必须同时给 domain 与 path，这里替调用方补上，
    免得每个调用点都写一遍那三行。
    """
    dropped = []
    for cookie in list(jar):
        if cookie.name in names:
            jar.clear(domain=cookie.domain, path=cookie.path, name=cookie.name)
            dropped.append(cookie.name)
    return dropped


class Cookies(RequestsCookieJar):
    def __repr__(self) -> str:
        return '; '.join(f'{key}{f"={value}" if value is not None else ""}' for key, value in self.items())

    def __str__(self) -> str:
        return repr(self)

    @classmethod
    def from_dict(cls, cookie_dict: dict[str, str | None]) -> Cookies:
        instance = cls()
        if cookie_dict is not None:
            for key, value in cookie_dict.items():
                instance.set(name=key, value=value)
        return instance

    @classmethod
    def from_session(cls, session: Session) -> Cookies:
        return cls.from_dict(session.cookies.get_dict())

    @classmethod
    def from_str(cls, cookie_str: str) -> Cookies:
        cookie_dict = {}
        for cookie in cookie_str.split('; '):
            key, *value = cookie.split('=')
            cookie_dict[key] = '='.join(value)
        return cls.from_dict(cookie_dict)

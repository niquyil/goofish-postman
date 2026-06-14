from __future__ import annotations

from typing import TYPE_CHECKING

from requests.cookies import RequestsCookieJar

if TYPE_CHECKING:
    from requests import Session


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

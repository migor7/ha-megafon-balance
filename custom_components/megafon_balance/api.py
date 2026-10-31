"""Megafon personal account (lk.megafon.ru) client.

Uses the API of the Megafon mobile app / web cabinet, the same way the
AnyBalance provider "ab-megafon" does (modules/extract/abe-cell-megafon/source/mobileapi.js):
  GET  mlk/api/auth/sessionCheck  -> {"authenticated", "phone", "jwtToken"}
  POST mlk/login (form: login, password) -> {"jwtToken"} or {"code", "message"}
  GET  mlk/api/main/balance       -> {"balance", "balanceWithLimit"|"limit", ...}
Error codes: a211 = captcha required, a216 = account blocked.
Unofficial: Megafon may change it at any time.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import aiohttp
from yarl import URL

_LOGGER = logging.getLogger(__name__)

API = "https://api.megafon.ru/mlk/"
TIMEOUT = aiohttp.ClientTimeout(total=30)

HEADERS = {
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://lk.megafon.ru",
    "User-Agent": (
        "Mozilla/5.0 (Linux; Android 8.0.0; AUM-L29 Build/HONORAUM-L29; wv) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 "
        "Chrome/106.0.5249.126 Mobile Safari/537.36"
    ),
    "X-App-Type": "webView",
    "X-Requested-With": "ru.megafon.mlk",
}

CODE_CAPTCHA = "a211"
CODE_BLOCKED = "a216"


class MegafonError(Exception):
    """Generic error."""


class MegafonAuthError(MegafonError):
    """Wrong number or password."""


class MegafonCaptchaError(MegafonError):
    """Megafon asks for a captcha; log in once in a browser and retry later."""


class MegafonBlockedError(MegafonError):
    """Account is blocked by Megafon (unblock via USSD or support)."""


class MegafonClient:
    def __init__(self, session: aiohttp.ClientSession, login: str, password: str) -> None:
        self._session = session
        self._login = login
        self._password = password
        self._headers = dict(HEADERS)
        self._jwt: str | None = None
        self._cabinet_headers_loaded = False

    async def balance(self) -> dict[str, Any]:
        """Balance of the login number: {"balance": float, "available": float|None}."""
        await self._ensure_login()
        try:
            return await self._current_balance()
        except MegafonError:
            # The session may have expired between calls: log in once more.
            await self._ensure_login(force=True)
            return await self._current_balance()

    async def all_balances(
        self, only: set[str] | None = None, known: set[str] | None = None
    ) -> dict[str, dict[str, Any]]:
        """Balances of the login number and every number linked to it
        ("Мультиаккаунт" in the cabinet), keyed by 10-digit number.
        With `only`, read just those numbers, plus linked numbers not in `known`
        (newly linked in the cabinet), so new numbers appear without a restart.

        The cabinet switches the whole session to a linked number, so read each
        one after switching and switch back to the login number at the end.
        """
        result: dict[str, dict[str, Any]] = {}
        if only is None or self._login in only:
            result[self._login] = await self.balance()
        else:
            await self._ensure_login()
        linked = [
            n for n in await self.linked_numbers()
            if n != self._login
            and (only is None or n in only or (known is not None and n not in known))
        ]
        try:
            for number in linked:
                try:
                    await self._switch(number)
                    result[number] = await self._current_balance()
                except MegafonError as err:
                    _LOGGER.warning("Balance of %s not read: %s", number, err)
        finally:
            if linked:
                try:
                    await self._switch(self._login)
                except MegafonError as err:
                    _LOGGER.debug("Switch back to %s failed: %s", self._login, err)
        return result

    async def linked_numbers(self) -> list[str]:
        """Numbers the login number may switch to.

        Reply: {"original": {"msisdn", "slavesAvailableToSwitch": [{"msisdn", "segment", "name"}]},
                "current": {"msisdn", "slaves": [{"msisdn", "name"}]}}
        """
        data = await self._call("get", "api/multiaccount/summary", allow_error=True)
        if data.get("code"):
            _LOGGER.debug("No multiaccount: %s", data)
            return []
        slaves = (data.get("original") or {}).get("slavesAvailableToSwitch")
        if isinstance(slaves, list):
            return _find_numbers(slaves)
        return _find_numbers(data)

    async def _switch(self, number: str) -> None:
        reply = await self._call("post", "api/multiaccount/change", form={"msisdn": number})
        if reply.get("jwtToken"):
            self._jwt = reply["jwtToken"]

    async def _current_balance(self) -> dict[str, Any]:
        data = await self._call("get", "api/main/balance")
        balance = _number(data.get("balance"))
        if balance is None:
            raise MegafonError(f"no balance in reply: {data!r}")
        return {
            "balance": balance,
            "available": _number(data.get("balanceWithLimit") or data.get("limit")),
        }

    async def _ensure_login(self, force: bool = False) -> None:
        if not self._cabinet_headers_loaded:
            await self._load_cabinet_headers()
        if not force:
            check = await self._call("get", "api/auth/sessionCheck", allow_error=True)
            if check.get("authenticated"):
                self._jwt = check.get("jwtToken") or self._jwt
                if str(check.get("phone")) == self._login:
                    return
                # Left on a linked number (e.g. HA restarted mid-poll): switch back.
                try:
                    await self._switch(self._login)
                    return
                except MegafonError:
                    pass
        reply = await self._call(
            "post", "login", form={"login": self._login, "password": self._password},
            allow_error=True,
        )
        code = reply.get("code")
        if code == CODE_CAPTCHA:
            raise MegafonCaptchaError(reply.get("message") or "captcha")
        if code == CODE_BLOCKED:
            raise MegafonBlockedError(reply.get("message") or "blocked")
        if code:
            message = reply.get("message") or code
            if re.search("неверн.{0,20}парол|парол.{0,20}неверн", message, re.I):
                raise MegafonAuthError(message)
            raise MegafonError(message)
        if not reply.get("jwtToken"):
            raise MegafonError(f"no token in login reply: {reply!r}")
        self._jwt = reply["jwtToken"]

    async def _load_cabinet_headers(self) -> None:
        """The web cabinet sends three X-Cabinet-* headers taken from its app bundle."""
        self._cabinet_headers_loaded = True
        try:
            async with self._session.get(
                "https://lk.megafon.ru/login", headers=self._headers, timeout=TIMEOUT
            ) as resp:
                html = await resp.text()
            m = re.search(r"""<script[^>]+src=['"]/public/rwlk/app\.([\s\S]*?)\.js['"]""", html)
            if not m:
                return
            async with self._session.get(
                f"https://lk.megafon.ru/public/rwlk/app.{m.group(1)}.js",
                headers=self._headers,
                timeout=TIMEOUT,
            ) as resp:
                js = await resp.text()
            for name in ("X-Cabinet-Id-Param", "X-Cabinet-Check-Info", "X-Cabinet-Validation-Param"):
                hm = re.search(rf'"{name}":\s*?"([^"]*)', js)
                if hm:
                    self._headers[name] = hm.group(1)
        except (aiohttp.ClientError, TimeoutError) as err:
            _LOGGER.debug("Could not read cabinet headers: %s", err)

    async def _call(
        self, method: str, path: str, form: dict | None = None, allow_error: bool = False
    ) -> dict[str, Any]:
        headers = dict(self._headers)
        if self._jwt:
            headers["X-Cabinet-Authorization"] = f"Bearer {self._jwt}"
        try:
            if method == "post":
                headers["Content-Type"] = "application/x-www-form-urlencoded;charset=UTF-8"
                # Like the web cabinet (axios xsrf settings): echo the CSRF cookie on
                # every non-GET request, otherwise e.g. multiaccount/change is refused.
                cookies = self._session.cookie_jar.filter_cookies(URL(API))
                for name in ("csrf-token", "NEW-CSRF-TOKEN"):
                    if name in cookies:
                        headers["X-CSRF-TOKEN"] = cookies[name].value
                        break
                req = self._session.post(API + path, data=form, headers=headers, timeout=TIMEOUT)
            else:
                req = self._session.get(API + path, headers=headers, timeout=TIMEOUT)
            async with req as resp:
                text = await resp.text()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise MegafonError(f"{path}: {err}") from err
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            raise MegafonError(f"{path}: HTTP {status}, not JSON") from None
        if not isinstance(data, dict):
            data = {"value": data}
        if data.get("code") == CODE_BLOCKED:
            raise MegafonBlockedError(data.get("message") or "blocked")
        if data.get("code") and not allow_error:
            raise MegafonError(f"{path}: {data.get('message') or data['code']}")
        return data


def _number(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return round(float(str(value).replace(",", ".").replace(" ", "")), 2)
    except ValueError:
        return None


def _find_numbers(data: Any) -> list[str]:
    """Collect phone numbers from the multiaccount summary, whatever its nesting."""
    found: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                if key.lower() in ("msisdn", "phone", "number", "phonenumber") and isinstance(
                    value, (str, int)
                ):
                    number = normalize_login(str(value))
                    if len(number) == 10 and number not in found:
                        found.append(number)
                else:
                    walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return found


def pretty(number: str) -> str:
    """9991234567 -> +7 999 123-45-67."""
    return f"+7 {number[:3]} {number[3:6]}-{number[6:8]}-{number[8:]}"


def normalize_login(value: str) -> str:
    """+7 999 123-45-67 / 89991234567 -> 9991234567."""
    digits = re.sub(r"\D", "", value)
    if len(digits) == 11 and digits[0] in "78":
        digits = digits[1:]
    return digits

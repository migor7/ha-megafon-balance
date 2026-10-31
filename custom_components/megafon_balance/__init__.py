"""Мегафон: баланс — SIM balance from the Megafon personal account."""
from __future__ import annotations

from datetime import datetime, timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    MegafonAuthError,
    MegafonBlockedError,
    MegafonCaptchaError,
    MegafonClient,
    MegafonError,
)

DOMAIN = "megafon_balance"
PLATFORMS = ["sensor"]

# Schedule: every N hours, or every N days at a given hour (7 = weekly,
# 30/31 = monthly). Anything from once an hour to once a month.
# The general schedule is stored flat in the options; per-number overrides in
# options["per_number"][<10-digit number>] = {every, unit, at_hour}.
CONF_EVERY = "every"
CONF_UNIT = "unit"
CONF_AT_HOUR = "at_hour"
CONF_PER_NUMBER = "per_number"
UNITS = ["hours", "days"]
MAX_EVERY = {"hours": 744, "days": 31}
DEFAULT_EVERY = 6
DEFAULT_UNIT = "hours"
DEFAULT_AT_HOUR = 9
# Pause before the next login after the 1st, 2nd, ... refusal in a row.
BACKOFF = [
    timedelta(hours=1),
    timedelta(hours=3),
    timedelta(hours=6),
    timedelta(hours=12),
    timedelta(hours=24),
]

_LOGGER = logging.getLogger(__name__)


def schedule_for(options: dict, number: str) -> dict:
    own = (options.get(CONF_PER_NUMBER) or {}).get(number)
    return own or {
        CONF_EVERY: options.get(CONF_EVERY, DEFAULT_EVERY),
        CONF_UNIT: options.get(CONF_UNIT, DEFAULT_UNIT),
        CONF_AT_HOUR: options.get(CONF_AT_HOUR, DEFAULT_AT_HOUR),
    }


def due_now(schedule: dict, now: datetime, last: datetime | None) -> bool:
    """Checked on every hour: is an update of a number with `schedule` due?"""
    every = int(schedule.get(CONF_EVERY, DEFAULT_EVERY))
    if schedule.get(CONF_UNIT, DEFAULT_UNIT) == "hours":
        # A few minutes of slack: the check runs on the hour, the read takes time.
        return last is None or now - last >= timedelta(hours=every) - timedelta(minutes=5)
    if now.hour != int(schedule.get(CONF_AT_HOUR, DEFAULT_AT_HOUR)):
        return False
    if last is None:
        return True
    # An hour of slack so the update stays on its hour from day to day.
    return now - last >= timedelta(days=every) - timedelta(hours=1)


class MegafonCoordinator(DataUpdateCoordinator[dict]):
    """Data: {number: {"balance", "available"}}. Every number keeps its own
    schedule; an hourly tick reads only the numbers that are due."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(hass, _LOGGER, name=DOMAIN, update_interval=None)
        self.entry = entry
        self.login = entry.data[CONF_USERNAME]
        self.last_read: dict[str, datetime] = {}
        self._only: set[str] | None = None  # None = all numbers
        # Own cookie jar per number: the cabinet session lives in cookies + JWT.
        self.client = MegafonClient(
            async_create_clientsession(hass), self.login, entry.data[CONF_PASSWORD]
        )
        # Back-off after Megafon refuses: repeated logins only make it block longer.
        # Kept on disk so HA restarts do not hammer the cabinet either.
        self._store = pause_store(hass, entry.entry_id)
        self.failures = 0
        self.paused_until: datetime | None = None

    async def async_load_pause(self) -> None:
        saved = await self._store.async_load() or {}
        self.failures = int(saved.get("failures", 0))
        until = saved.get("paused_until")
        self.paused_until = dt_util.parse_datetime(until) if until else None

    def paused(self, now: datetime) -> bool:
        return self.paused_until is not None and now < self.paused_until

    def due_numbers(self, now: datetime) -> set[str] | None:
        """Numbers to read now; None = all (nothing read successfully yet)."""
        if not self.data:
            return None
        options = self.entry.options
        return {
            number
            for number in self.data
            if due_now(schedule_for(options, number), now, self.last_read.get(number))
        }

    async def async_refresh_numbers(self, numbers: set[str] | None) -> None:
        self._only = numbers
        try:
            await self.async_refresh()
        finally:
            self._only = None

    async def _pause_after_failure(self) -> str:
        delay = BACKOFF[min(self.failures, len(BACKOFF) - 1)]
        self.failures += 1
        self.paused_until = dt_util.now() + delay
        await self._store.async_save(
            {"failures": self.failures, "paused_until": self.paused_until.isoformat()}
        )
        return f"следующая попытка входа после {dt_util.as_local(self.paused_until):%d.%m %H:%M}"

    async def _async_update_data(self) -> dict:
        if self.paused(dt_util.now()):
            raise UpdateFailed(
                "Пауза после отказа Мегафона до "
                f"{dt_util.as_local(self.paused_until):%d.%m %H:%M}"
            )
        try:
            fresh = await self.client.all_balances(self._only, set(self.data or {}))
        except MegafonAuthError as err:
            await self._pause_after_failure()
            raise ConfigEntryAuthFailed(str(err)) from err
        except MegafonCaptchaError as err:
            when = await self._pause_after_failure()
            raise UpdateFailed(
                "Мегафон просит капчу. Войдите один раз в lk.megafon.ru в браузере "
                f"с этим номером; {when}."
            ) from err
        except MegafonError as err:
            when = await self._pause_after_failure()
            raise UpdateFailed(f"{err}; {when}") from err
        if self.failures:
            self.failures, self.paused_until = 0, None
            await self._store.async_remove()
        now = dt_util.now()
        for number in fresh:
            self.last_read[number] = now
        # Keep the last values of numbers that were not due this time.
        return {**(self.data or {}), **fresh}


def pause_store(hass: HomeAssistant, entry_id: str) -> Store:
    return Store(hass, 1, f"{DOMAIN}.{entry_id}.pause")


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = MegafonCoordinator(hass, entry)
    await coordinator.async_load_pause()
    # Not async_config_entry_first_refresh: a refusal must not make HA retry the
    # setup (and the login) every few seconds. Sensors appear on the first success.
    if not coordinator.paused(dt_util.now()):
        await coordinator.async_refresh()  # all numbers at startup
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    async def on_the_hour(now: datetime) -> None:
        if coordinator.paused(dt_util.now()):
            return
        due = coordinator.due_numbers(dt_util.as_local(now))
        if due is None or due:
            await coordinator.async_refresh_numbers(due)

    entry.async_on_unload(async_track_time_change(hass, on_the_hour, minute=0, second=0))
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        hass.data[DOMAIN].pop(entry.entry_id)
    return ok

from __future__ import annotations

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import callback
from homeassistant.helpers import device_registry as dr, selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession

from . import (
    CONF_AT_HOUR,
    CONF_EVERY,
    CONF_PER_NUMBER,
    CONF_UNIT,
    DEFAULT_AT_HOUR,
    DEFAULT_EVERY,
    DEFAULT_UNIT,
    DOMAIN,
    MAX_EVERY,
    UNITS,
    pause_store,
    schedule_for,
)
from .api import (
    MegafonAuthError,
    MegafonBlockedError,
    MegafonCaptchaError,
    MegafonClient,
    MegafonError,
    normalize_login,
    pretty,
)

CONF_NUMBER = "number"
CONF_OWN = "own_schedule"  # off = the number follows the general schedule


class MegafonConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    _message = ""  # Megafon's own error text, shown in the form

    async def _check(self, login: str, password: str) -> str | None:
        """Return an error key, or None if the login works."""
        client = MegafonClient(async_create_clientsession(self.hass), login, password)
        try:
            await client.balance()  # login + own balance; linked numbers come later
        except MegafonCaptchaError as err:
            self._message = str(err)
            return "captcha"
        except MegafonError as err:
            # Wrong password, blocked, too many attempts...: show Megafon's text as is.
            self._message = str(err)
            return "megafon"
        return None

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            login = normalize_login(user_input[CONF_USERNAME])
            password = user_input[CONF_PASSWORD]
            if len(login) != 10:
                errors[CONF_USERNAME] = "bad_number"
            else:
                await self.async_set_unique_id(login)
                self._abort_if_unique_id_configured()
                if not (error := await self._check(login, password)):
                    return self.async_create_entry(
                        title=pretty(login),
                        data={CONF_USERNAME: login, CONF_PASSWORD: password},
                    )
                errors["base"] = error
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {vol.Required(CONF_USERNAME): str, vol.Required(CONF_PASSWORD): str}
            ),
            errors=errors,
            description_placeholders={"message": self._message},
        )

    async def async_step_reauth(self, entry_data):
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input=None):
        entry = self._get_reauth_entry()
        errors = {}
        if user_input is not None:
            password = user_input[CONF_PASSWORD]
            if not (error := await self._check(entry.data[CONF_USERNAME], password)):
                # The login just worked: drop the back-off pause.
                await pause_store(self.hass, entry.entry_id).async_remove()
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_PASSWORD: password}
                )
            errors["base"] = error
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): str}),
            errors=errors,
            description_placeholders={"message": self._message},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return MegafonOptionsFlow()


def _number(lo, hi):
    return selector.NumberSelector(
        selector.NumberSelectorConfig(min=lo, max=hi, step=1, mode=selector.NumberSelectorMode.BOX)
    )


def _schedule_schema(current: dict, with_own_switch: bool = False) -> vol.Schema:
    fields = {}
    if with_own_switch:
        fields[vol.Required(CONF_OWN, default=current.get(CONF_OWN, False))] = bool
    fields.update(
        {
            vol.Required(CONF_EVERY, default=current.get(CONF_EVERY, DEFAULT_EVERY)): _number(1, 744),
            vol.Required(CONF_UNIT, default=current.get(CONF_UNIT, DEFAULT_UNIT)): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=UNITS, translation_key="unit", mode=selector.SelectSelectorMode.LIST
                )
            ),
            vol.Required(CONF_AT_HOUR, default=current.get(CONF_AT_HOUR, DEFAULT_AT_HOUR)): _number(0, 23),
        }
    )
    return vol.Schema(fields)


def _clean_schedule(user_input: dict) -> tuple[dict, dict]:
    """Return (schedule, errors)."""
    schedule = {
        CONF_EVERY: int(user_input[CONF_EVERY]),
        CONF_UNIT: user_input[CONF_UNIT],
        CONF_AT_HOUR: int(user_input[CONF_AT_HOUR]),
    }
    errors = {}
    if schedule[CONF_EVERY] > MAX_EVERY[schedule[CONF_UNIT]]:
        errors[CONF_EVERY] = "too_rare"
    return schedule, errors


class MegafonOptionsFlow(config_entries.OptionsFlow):
    """General schedule for all numbers, and an own schedule for any number.

    Each: every N hours, or every N days at an hour (up to once a month).
    """

    def __init__(self) -> None:
        self._number: str | None = None

    async def async_step_init(self, user_input=None):
        return self.async_show_menu(step_id="init", menu_options=["general", "pick_number"])

    async def async_step_general(self, user_input=None):
        errors = {}
        if user_input is not None:
            schedule, errors = _clean_schedule(user_input)
            if not errors:
                return self.async_create_entry(data={**self.config_entry.options, **schedule})
        current = {**self.config_entry.options, **(user_input or {})}
        return self.async_show_form(
            step_id="general", data_schema=_schedule_schema(current), errors=errors
        )

    def _known_numbers(self) -> list[str]:
        """The login number, numbers read this run, and every number HA has a device
        for (so the list is not empty while Megafon refuses and nothing is read)."""
        entry = self.config_entry
        numbers = {entry.data[CONF_USERNAME]}
        numbers |= set(entry.options.get(CONF_PER_NUMBER) or {})
        coordinator = self.hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if coordinator and coordinator.data:
            numbers |= set(coordinator.data)
        for device in dr.async_entries_for_config_entry(dr.async_get(self.hass), entry.entry_id):
            numbers |= {ident for domain, ident in device.identifiers if domain == DOMAIN}
        return sorted(numbers)

    async def async_step_pick_number(self, user_input=None):
        if user_input is not None:
            self._number = user_input[CONF_NUMBER]
            return await self.async_step_number_schedule()
        per_number = self.config_entry.options.get(CONF_PER_NUMBER) or {}
        options = [
            selector.SelectOptionDict(
                value=n, label=pretty(n) + (" ✱" if n in per_number else "")
            )
            for n in self._known_numbers()
        ]
        return self.async_show_form(
            step_id="pick_number",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NUMBER): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options, mode=selector.SelectSelectorMode.LIST
                        )
                    )
                }
            ),
        )

    async def async_step_number_schedule(self, user_input=None):
        number = self._number
        options = self.config_entry.options
        per_number = dict(options.get(CONF_PER_NUMBER) or {})
        errors = {}
        if user_input is not None and CONF_EVERY in user_input:
            if not user_input[CONF_OWN]:
                per_number.pop(number, None)
                return self.async_create_entry(data={**options, CONF_PER_NUMBER: per_number})
            schedule, errors = _clean_schedule(user_input)
            if not errors:
                per_number[number] = schedule
                return self.async_create_entry(data={**options, CONF_PER_NUMBER: per_number})
        own = per_number.get(number)
        current = {**(own or schedule_for(options, number)), CONF_OWN: own is not None}
        if user_input is not None and CONF_EVERY in user_input:
            current.update(user_input)
        return self.async_show_form(
            step_id="number_schedule",
            data_schema=_schedule_schema(current, with_own_switch=True),
            description_placeholders={"number": pretty(number)},
            errors=errors,
        )

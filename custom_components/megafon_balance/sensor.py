from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN
from .api import pretty


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]
    known: set[str] = set()

    @callback
    def add_new() -> None:
        # Numbers linked in the cabinet later appear on the next update.
        new = [n for n in (coordinator.data or {}) if n not in known]
        known.update(new)
        if new:
            async_add_entities(BalanceSensor(coordinator, n) for n in new)

    add_new()
    entry.async_on_unload(coordinator.async_add_listener(add_new))


class BalanceSensor(CoordinatorEntity, SensorEntity):
    """Balance of one number: the login number or one linked to it."""

    _attr_has_entity_name = True
    _attr_translation_key = "balance"
    _attr_icon = "mdi:cash"
    _attr_native_unit_of_measurement = "RUB"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 2

    def __init__(self, coordinator, number: str) -> None:
        super().__init__(coordinator)
        self._number = number
        self._attr_unique_id = f"{number}_balance"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, number)},
            manufacturer="Мегафон",
            model="SIM",
            name=f"Мегафон {pretty(number)}",
        )

    @property
    def _data(self) -> dict:
        return (self.coordinator.data or {}).get(self._number) or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self._data)

    @property
    def native_value(self):
        return self._data.get("balance")

    @property
    def extra_state_attributes(self):
        available = self._data.get("available")
        return {"available_with_limit": available} if available is not None else None

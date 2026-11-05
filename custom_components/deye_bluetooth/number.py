"""Numbers on the device page: the settings of registers.NUMBERS."""
from __future__ import annotations

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeyeCoordinator
from . import registers as r
from .entity import ControlEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(SettingNumber(entry.runtime_data, key, limits) for key, limits in r.NUMBERS.items())


class SettingNumber(ControlEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: DeyeCoordinator, key: str, limits: tuple) -> None:
        super().__init__(coordinator, key, "number")
        self._attr_native_min_value, self._high, self._attr_native_step = limits
        self._attr_native_unit_of_measurement = self._value.unit

    @property
    def native_max_value(self) -> float:
        if self._high is r.RATED:
            return r.rated_power(self.regs) or r.DEFAULT_RATED_W
        return self._high

    @property
    def native_value(self):
        return r.decode_value(self._value, self.regs)

    async def async_set_native_value(self, value: float) -> None:
        await self._write(r.encode_value(self._value, value))

"""Numbers on the device page: the settings of the register map's numbers."""
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
    coordinator = entry.runtime_data
    async_add_entities(SettingNumber(coordinator, key, limits) for key, limits in coordinator.map.numbers.items())


class SettingNumber(ControlEntity, NumberEntity):
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: DeyeCoordinator, key: str, limits: tuple) -> None:
        super().__init__(coordinator, key, "number")
        self._attr_native_min_value, self._high, self._attr_native_step = limits
        self._attr_native_unit_of_measurement = self._value.unit

    @property
    def native_max_value(self) -> float:
        if self._high is r.RATED:
            return r.rated_power(self.regs, self.coordinator.map) or r.DEFAULT_RATED_W
        return self._high

    async def async_set_native_value(self, value: float) -> None:
        await self._write(r.encode_value(self._value, value))

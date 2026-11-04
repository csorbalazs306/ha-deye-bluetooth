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
        self._limits = limits
        self._attr_native_unit_of_measurement = self._value.unit

    def _limit(self, limit: float | str | None) -> float:
        if limit is r.RATED:
            return r.rated_power(self.regs) or 0
        return self.coordinator.option(limit) if isinstance(limit, str) else limit

    @property
    def native_min_value(self) -> float:
        return self._limit(self._limits[0])

    @property
    def native_max_value(self) -> float:
        return self._limit(self._limits[1])

    @property
    def native_step(self) -> float:
        return self._limit(self._limits[2])

    @property
    def available(self) -> bool:
        return super().available and self.native_max_value > self.native_min_value

    @property
    def native_value(self):
        return r.decode_value(self._value, self.regs)

    async def async_set_native_value(self, value: float) -> None:
        await self._write(r.encode_value(self._value, value))

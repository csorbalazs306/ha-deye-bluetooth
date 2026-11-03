"""The Time-of-Use programs' start times on the device page."""
from __future__ import annotations

from datetime import time

from homeassistant.components.time import TimeEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeyeCoordinator
from . import registers as r
from .entity import ControlEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(ProgramTime(entry.runtime_data, key) for key in r.TIMES)


class ProgramTime(ControlEntity, TimeEntity):
    def __init__(self, coordinator: DeyeCoordinator, key: str) -> None:
        super().__init__(coordinator, key, "time")

    @property
    def native_value(self) -> time | None:
        raw = self.regs.get(self._value.reg)
        if raw is None or raw // 100 > 23 or raw % 100 > 59:
            return None
        return time(raw // 100, raw % 100)

    async def async_set_value(self, value: time) -> None:
        await self._write(value.hour * 100 + value.minute)

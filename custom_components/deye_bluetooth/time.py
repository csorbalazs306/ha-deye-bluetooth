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
        hhmm = r.decode_hhmm(self.regs.get(self._value.reg))
        if hhmm is None or hhmm[0] > 23 or hhmm[1] > 59:  # the range is checked here, not in the decoding
            return None
        return time(*hhmm)

    async def async_set_value(self, value: time) -> None:
        await self._write(r.encode_hhmm(value.hour, value.minute))

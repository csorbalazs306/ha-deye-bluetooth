"""Dropdowns on the device page: the settings of registers.SELECTS."""
from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeyeCoordinator
from . import registers as r
from .entity import ControlEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities(SettingSelect(entry.runtime_data, key, options) for key, options in r.SELECTS.items())


class SettingSelect(ControlEntity, SelectEntity):
    def __init__(self, coordinator: DeyeCoordinator, key: str, options: dict[int, str]) -> None:
        super().__init__(coordinator, key, "select")
        self._by_raw = options
        self._by_text = {text: raw for raw, text in options.items()}
        self._attr_options = list(options.values())

    @property
    def current_option(self) -> str | None:
        return self._by_raw.get(self.raw)

    async def async_select_option(self, option: str) -> None:
        await self._write(self._by_text[option])

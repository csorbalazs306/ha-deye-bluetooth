"""binary_sensor.deye_grid: on = the grid is there.

Off only when both say so: the grid relay is open (register 552, bit 2) and no
grid phase is above GRID_PRESENT_MIN_V.  An open relay with the grid present
(the inverter's self-test after a restart) is not an outage.
Stale data (the Bluetooth link is down) is unavailable, never "off".
"""
from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeyeCoordinator
from . import registers as r
from .entity import DeyeEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    async_add_entities([GridSensor(entry.runtime_data)])


class GridSensor(DeyeEntity, BinarySensorEntity):
    _attr_name = "Grid"
    _attr_device_class = BinarySensorDeviceClass.POWER

    def __init__(self, coordinator: DeyeCoordinator) -> None:
        super().__init__(coordinator, "deye_grid", "binary_sensor.deye_grid")

    @property
    def available(self) -> bool:
        return super().available and 552 in self.regs and self._volts() is not None

    def _volts(self) -> float | None:
        volts = [r.decode_value(r.BY_KEY[f"grid_voltage_l{i}"], self.regs) for i in (1, 2, 3)]
        return None if None in volts else max(volts)

    @property
    def is_on(self) -> bool:
        return bool(self.regs.get(552, 0) & r.GRID_RELAY_BIT) or (self._volts() or 0) >= r.GRID_PRESENT_MIN_V

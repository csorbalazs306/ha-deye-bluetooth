"""binary_sensor.deye_grid: on = the grid is there.

Off only when both say so: the grid relay is open (3-phase map: register 552, bit 2;
single-phase map: register 194 is 0) and no grid phase is above GRID_PRESENT_MIN_V.  An open relay with the grid present
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
        super().__init__(coordinator, "deye_grid", "binary_sensor", "_grid")

    @property
    def available(self) -> bool:
        return super().available and self.coordinator.map.relay_reg in self.regs and self._volts() is not None

    def _volts(self) -> float | None:
        volts = [r.decode_value(v, self.regs) for v in self.coordinator.map.grid_voltages]
        return None if None in volts else max(volts)

    @property
    def is_on(self) -> bool:
        m = self.coordinator.map
        relay_closed = bool(self.regs.get(m.relay_reg, 0) & m.relay_mask)
        return relay_closed or (self._volts() or 0) >= r.GRID_PRESENT_MIN_V

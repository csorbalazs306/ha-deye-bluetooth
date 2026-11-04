"""switch.deye_grid_charge: the battery charging from the grid.

On: all six Time-of-Use programs charge from the grid and the inverter's Grid
Charge is enabled.  Off: no program charges from the grid and Grid Charge is
disabled.  Nothing else is written: each program keeps its own SoC, which is how far
it charges and how far the battery covers the house.  The state is read back
from the inverter (Grid Charge and program 1's grid flag).
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeyeCoordinator
from . import registers as r
from .entity import ControlEntity, DeyeEntity

GRID_FLAG = 0x01  # a program's charge register: bit 0 = from the grid


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator = entry.runtime_data
    async_add_entities([GridChargeSwitch(coordinator), *(SettingSwitch(coordinator, k) for k in r.ON_OFF_SWITCHES)])


class _ToggleSwitch(SwitchEntity):
    """A switch whose two services go through one write."""

    async def async_turn_on(self, **kwargs: Any) -> None:
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        await self._set(False)

    async def _set(self, on: bool) -> None:
        raise NotImplementedError


class GridChargeSwitch(DeyeEntity, _ToggleSwitch):
    _attr_name = "Grid charge"
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, coordinator: DeyeCoordinator) -> None:
        super().__init__(coordinator, "deye_grid_charge", "switch", "deye_grid_charge")

    @property
    def available(self) -> bool:
        return super().available and self._present(r.REG_GRID_CHARGE, r.PROGRAM_CHARGE)

    @property
    def is_on(self) -> bool:
        return bool(self.regs.get(r.REG_GRID_CHARGE)) and bool(self.regs.get(r.PROGRAM_CHARGE, 0) & GRID_FLAG)

    async def _set(self, on: bool) -> None:
        flags = [
            (w | GRID_FLAG) if on else (w & ~GRID_FLAG)
            for w in (self.regs.get(r.PROGRAM_CHARGE + i, 0) for i in range(6))
        ]
        await self.coordinator.write(r.PROGRAM_CHARGE, flags)
        await self.coordinator.write(r.REG_GRID_CHARGE, [1 if on else 0])


class SettingSwitch(ControlEntity, _ToggleSwitch):
    def __init__(self, coordinator: DeyeCoordinator, key: str) -> None:
        super().__init__(coordinator, key, "switch")

    @property
    def is_on(self) -> bool:
        return bool(self.raw)

    async def _set(self, on: bool) -> None:
        await self._write(1 if on else 0)

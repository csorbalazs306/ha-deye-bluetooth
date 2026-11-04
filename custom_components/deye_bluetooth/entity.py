"""What the platforms create, and the shared entity base."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DeyeCoordinator
from . import registers as r

POWER = (SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT)
ENERGY = (SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING)
CURRENT = (SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT)
VOLTAGE = (SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT)
UNIT_CLASSES = {
    "W": POWER,
    "kWh": ENERGY,
    "A": CURRENT,
    "V": VOLTAGE,
    "Hz": (SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT),
    "°C": (SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
    "%": (None, SensorStateClass.MEASUREMENT),
}


@dataclass(frozen=True)
class SummarySensor:
    """One of the summary sensors with a short, stable id."""

    object_id: str
    name: str
    value: Callable[[dict[int, int]], float | int | None]
    unit: str | None
    classes: tuple = (None, SensorStateClass.MEASUREMENT)
    live: bool = False  # written every live interval (1 s), else with the 15 s poll


def _key(key: str, scale: float = 1.0) -> Callable[[dict[int, int]], Any]:
    v = r.BY_KEY[key]

    def read(regs: dict[int, int]):
        value = r.decode_value(v, regs)
        return None if value is None else (round(value * scale, 3) if scale != 1.0 else value)

    return read


def _summary(object_id, name, key, unit, classes=(None, SensorStateClass.MEASUREMENT), scale=1.0, live=False) -> SummarySensor:
    return SummarySensor(object_id, name, _key(key, scale), unit, classes, live)


SUMMARY_SENSORS: list[SummarySensor] = [
    *[
        sensor
        for ph, n in (("a", 1), ("b", 2), ("c", 3))
        for sensor in (
            # current signed like the power (export < 0), power in kW
            _summary(f"deye_grid_phase_{ph}_current", f"Grid phase {ph.upper()} current", f"grid_current_l{n}", "A", CURRENT,
                 live=True),
            _summary(f"deye_grid_phase_{ph}_voltage", f"Grid phase {ph.upper()} voltage", f"grid_voltage_l{n}", "V", VOLTAGE),
            _summary(f"deye_grid_phase_{ph}_power", f"Grid phase {ph.upper()} power", f"grid_power_l{n}", "kW", POWER, 0.001,
                 live=True),
        )
    ],
    _summary("deye_grid_power", "Grid power", "grid_power", "W", POWER, live=True),
    _summary("deye_load_power", "Load power", "load_power", "kW", POWER, 0.001, live=True),
    SummarySensor("deye_pv_power", "PV power", r.pv_power, "W", POWER),
    _summary("deye_pv1_power", "PV1 power", "pv1_power", "W", POWER),
    _summary("deye_pv2_power", "PV2 power", "pv2_power", "W", POWER),
    _summary("deye_battery_soc", "Battery SoC", "battery_soc", "%", (SensorDeviceClass.BATTERY, SensorStateClass.MEASUREMENT)),
    _summary("deye_battery_power", "Battery power", "battery_power", "W", POWER),
    _summary("deye_battery_capacity", "Battery capacity", "battery_capacity", "Ah", (None, None)),
    _summary("deye_grid_import_energy", "Grid import energy", "total_import", "kWh", ENERGY),
    _summary("deye_grid_export_energy", "Grid export energy", "total_export", "kWh", ENERGY),
    _summary("deye_load_energy", "Load energy", "total_load", "kWh", ENERGY),
    _summary("deye_pv_energy", "PV energy", "total_pv", "kWh", ENERGY),
    _summary("deye_program_soc", "Program SoC", "program_1_soc", "%"),
    _summary("deye_solar_sell", "Solar sell", "solar_sell", None),
]
DETAIL_PREFIX = "deye_inverter_"


class DeyeEntity(CoordinatorEntity[DeyeCoordinator]):
    """Written with every poll that read more than the live blocks (15 s), a
    live entity every live interval (1 s): each state is a recorder row."""

    _attr_has_entity_name = True
    live = False

    def __init__(self, coordinator: DeyeCoordinator, key: str, domain: str, object_id: str) -> None:
        """object_id is the entity id of the first logger ("deye..."); the prefix replaces its "deye"."""
        super().__init__(coordinator)
        self.entity_id = f"{domain}.{coordinator.prefix}{object_id.removeprefix('deye')}"
        self._attr_unique_id = f"{coordinator.config_entry.unique_id}_{key}"
        self._attr_device_info = coordinator.device_info

    def _handle_coordinator_update(self) -> None:
        available = self.available
        if self.live or self.coordinator.full_poll or available != getattr(self, "_was_available", None):
            self._was_available = available
            super()._handle_coordinator_update()

    @property
    def regs(self) -> dict[int, int]:
        return self.coordinator.data or {}

    def _present(self, *addresses: int) -> bool:
        """True while the data holds every one of these registers."""
        return all(address in self.regs for address in addresses)


class ControlEntity(DeyeEntity):
    """One register the device page can change; it writes only when someone does."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DeyeCoordinator, key: str, domain: str) -> None:
        super().__init__(coordinator, f"{key}_control", domain, f"{DETAIL_PREFIX}{key}")
        self._value = r.BY_KEY[key]
        self._attr_name = self._value.name

    @property
    def raw(self) -> int | None:
        """The register's word, as the inverter has it."""
        return self.regs.get(self._value.reg)

    @property
    def available(self) -> bool:
        return super().available and self._present(self._value.reg)

    async def _write(self, raw: int) -> None:
        await self.coordinator.write(self._value.reg, [raw])

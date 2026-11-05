"""What the platforms create, and the shared entity base."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorStateClass
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import DOMAIN, DeyeCoordinator
from . import registers as r

POWER = (SensorDeviceClass.POWER, SensorStateClass.MEASUREMENT)
ENERGY = (SensorDeviceClass.ENERGY, SensorStateClass.TOTAL_INCREASING)
CURRENT = (SensorDeviceClass.CURRENT, SensorStateClass.MEASUREMENT)
VOLTAGE = (SensorDeviceClass.VOLTAGE, SensorStateClass.MEASUREMENT)
UNIT_CLASSES = {
    "W": POWER,
    "kW": POWER,
    "kWh": ENERGY,
    "A": CURRENT,
    "V": VOLTAGE,
    "Hz": (SensorDeviceClass.FREQUENCY, SensorStateClass.MEASUREMENT),
    "°C": (SensorDeviceClass.TEMPERATURE, SensorStateClass.MEASUREMENT),
    "%": (None, SensorStateClass.MEASUREMENT),
}
_DEFAULT_CLASSES = (None, SensorStateClass.MEASUREMENT)


@dataclass(frozen=True)
class SummarySensor:
    """One of the summary sensors with a short, stable id."""

    object_id: str  # "deye_grid_power": the unique id; the entity id is the logger's prefix plus the rest
    name: str
    value: Callable[[dict[int, int]], float | int | None]
    unit: str | None
    classes: tuple = _DEFAULT_CLASSES
    live: bool = False  # written every live interval (1 s), else with the 15 s poll
    source: str | None = None  # the register key the value is read from; None when it has several


def _key(key: str, scale: float = 1.0) -> Callable[[dict[int, int]], Any]:
    v = r.BY_KEY[key]

    def read(regs: dict[int, int]):
        value = r.decode_value(v, regs)
        return None if value is None else (round(value * scale, 3) if scale != 1.0 else value)

    return read


def _summary(
    object_id: str, name: str, key: str, *, unit: str | None = None, classes: tuple | None = None,
    scale: float = 1.0, live: bool = False,
) -> SummarySensor:
    """A row for one register: its unit and classes are the register's, unless given (kW, a battery, no class)."""
    unit = r.BY_KEY[key].unit if unit is None else unit
    classes = UNIT_CLASSES.get(unit, _DEFAULT_CLASSES) if classes is None else classes
    return SummarySensor(object_id, name, _key(key, scale), unit, classes, live, key)


SUMMARY_SENSORS: list[SummarySensor] = [
    *[
        sensor
        for ph, n in (("a", 1), ("b", 2), ("c", 3))
        for sensor in (
            # current signed like the power (export < 0), power in kW
            _summary(f"deye_grid_phase_{ph}_current", f"Grid phase {ph.upper()} current", f"grid_current_l{n}", live=True),
            _summary(f"deye_grid_phase_{ph}_voltage", f"Grid phase {ph.upper()} voltage", f"grid_voltage_l{n}"),
            _summary(f"deye_grid_phase_{ph}_power", f"Grid phase {ph.upper()} power", f"grid_power_l{n}", unit="kW",
                     scale=0.001, live=True),
        )
    ],
    _summary("deye_grid_power", "Grid power", "grid_power", live=True),
    _summary("deye_load_power", "Load power", "load_power", unit="kW", scale=0.001, live=True),
    SummarySensor("deye_pv_power", "PV power", r.pv_power, "W", POWER),  # the sum of four registers: no source
    _summary("deye_pv1_power", "PV1 power", "pv1_power"),
    _summary("deye_pv2_power", "PV2 power", "pv2_power"),
    _summary("deye_battery_soc", "Battery SoC", "battery_soc", classes=(SensorDeviceClass.BATTERY, SensorStateClass.MEASUREMENT)),
    _summary("deye_battery_power", "Battery power", "battery_power"),
    _summary("deye_battery_capacity", "Battery capacity", "battery_capacity", classes=(None, None)),
    _summary("deye_grid_import_energy", "Grid import energy", "total_import"),
    _summary("deye_grid_export_energy", "Grid export energy", "total_export"),
    _summary("deye_load_energy", "Load energy", "total_load"),
    _summary("deye_pv_energy", "PV energy", "total_pv"),
    _summary("deye_program_soc", "Program SoC", "program_1_soc"),
    _summary("deye_solar_sell", "Solar sell", "solar_sell"),
]
# the detail sensors that repeat a control or a summary sensor: disabled by default, the controls and summaries are the
# ones people use.  Enabling one shows the same register again.
DUPLICATES = frozenset({
    *r.NUMBERS, *r.SELECTS, *r.TIMES, *r.ON_OFF_SWITCHES, *(s.source for s in SUMMARY_SENSORS if s.source),
})


class DeyeEntity(CoordinatorEntity[DeyeCoordinator]):
    """The base of every entity of a logger.

    Written when its poll read its data: a live entity every live interval (1 s), a settings entity on a settings
    poll, any other on a fast poll (15 s); and whenever its availability changes.  Each state is a recorder row.
    """

    _attr_has_entity_name = True
    live = False  # written every live interval
    settings = False  # backed only by the settings registers: written on a settings poll
    _was_available: bool | None = None
    _seen: tuple[bool, frozenset[int]] | None = None  # (last_update_success, fresh_blocks) at the last look

    def __init__(self, coordinator: DeyeCoordinator, key: str, platform: str, suffix: str) -> None:
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.entity_id = f"{platform}.{coordinator.prefix}{suffix}"
        self._attr_unique_id = f"{entry.unique_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id)},
            name=f"Deye inverter{coordinator.prefix.removeprefix('deye').replace('_', ' ')}",
            manufacturer="Deye",
            model=r.device_model(coordinator.data),
            serial_number=r.decode_serial(coordinator.data),
        )

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        # the state is written when the entity is added: that is the look the next updates compare with
        self._seen = (self.coordinator.last_update_success, self.coordinator.fresh_blocks)
        self._was_available = self.available

    def _handle_coordinator_update(self) -> None:
        coordinator = self.coordinator
        if self.live:
            super()._handle_coordinator_update()
            return
        seen = (coordinator.last_update_success, coordinator.fresh_blocks)
        if (coordinator.slow_poll if self.settings else coordinator.full_poll):
            self._seen = seen
            self._was_available = self.available
            super()._handle_coordinator_update()
            return
        if seen == self._seen:
            return  # nothing this entity shows can have changed since it last looked
        self._seen = seen
        available = self.available
        if available != self._was_available:
            self._was_available = available
            super()._handle_coordinator_update()

    @property
    def regs(self) -> dict[int, int]:
        return self.coordinator.data


class DetailEntity(DeyeEntity):
    """An entity whose id is sensor.deye_inverter_<key> (or the control's domain), under the logger's prefix."""

    def __init__(self, coordinator: DeyeCoordinator, key: str, platform: str, unique_key: str | None = None) -> None:
        super().__init__(coordinator, unique_key or key, platform, f"_inverter_{key}")


class ValueEntity(DetailEntity):
    """One register of the map as an entity: its value decoded from the poll."""

    def __init__(self, coordinator: DeyeCoordinator, value: r.Value, platform: str, unique_key: str | None = None) -> None:
        super().__init__(coordinator, value.key, platform, unique_key)
        self._value = value
        self._attr_name = value.name
        self.settings = value.reg in r.SLOW_REGS

    @property
    def native_value(self):
        return r.decode_value(self._value, self.regs)


class ValueAvailable:
    """A value sensor is available while its value is there: the link's freshness first, then the value."""

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class ControlEntity(ValueEntity):
    """One register the device page can change; it writes only when someone does."""

    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DeyeCoordinator, key: str, domain: str) -> None:
        super().__init__(coordinator, r.BY_KEY[key], domain, unique_key=f"{key}_control")

    @property
    def available(self) -> bool:
        return super().available and self._value.reg in self.regs

    async def _write(self, raw: int) -> None:
        await self.coordinator.write(self._value.reg, [raw])

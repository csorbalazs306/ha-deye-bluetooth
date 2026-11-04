"""Sensors: the summary, every decoded register, and the few values built from several."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeyeCoordinator
from . import registers as r
from .entity import DETAIL_PREFIX, SUMMARY_SENSORS, UNIT_CLASSES, DeyeEntity, SummarySensor

DIAGNOSTIC_KEYS = ("relay_status", "relay_generator", "device_state", "device_alarm", "fault_1", "fault_2")

Decoder = Callable[[dict[int, int]], Any]


def _firmware(part: str) -> Decoder:
    """One firmware part, as the Deye app shows it."""
    return lambda regs: r.decode_firmware(regs, part)


def _choice(options: dict[int, str], reg: int) -> Decoder:
    """The text of one register word."""
    return lambda regs: r.text(options, regs.get(reg))


# the diagnostic values that are not one register: key, name, decoder
DERIVED: list[tuple[str, str, Decoder]] = [
    ("serial", "Serial number", r.decode_serial),
    ("work_mode_name", "Work mode name", _choice(r.WORK_MODES, r.WORK_MODE_REG)),
    *((f"firmware_{part}", f"Firmware {part}", _firmware(part)) for part in r.FIRMWARE),
]


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DeyeCoordinator = entry.runtime_data
    entities: list[SensorEntity] = [SummaryValueSensor(coordinator, s) for s in SUMMARY_SENSORS]
    entities += [DetailSensor(coordinator, v) for v in r.VALUES]
    entities += [DerivedSensor(coordinator, *spec) for spec in DERIVED]
    entities += [StateSensor(coordinator)]
    async_add_entities(entities)


class _ValueSensor(DeyeEntity, SensorEntity):
    """A sensor decoded from the registers: unavailable while its value cannot be decoded."""

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class SummaryValueSensor(_ValueSensor):
    def __init__(self, coordinator: DeyeCoordinator, desc: SummarySensor) -> None:
        super().__init__(coordinator, desc.object_id, "sensor", desc.object_id)
        self._desc = desc
        self.live = desc.live
        self._attr_name = desc.name
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class, self._attr_state_class = desc.classes

    @property
    def native_value(self):
        return self._desc.value(self.regs)


class DetailSensor(_ValueSensor):
    """sensor.deye_inverter_<key>: one register value as the inverter reports it."""

    def __init__(self, coordinator: DeyeCoordinator, value: r.Value) -> None:
        super().__init__(coordinator, value.key, "sensor", f"{DETAIL_PREFIX}{value.key}")
        self._value = value
        self._attr_name = value.name
        self._attr_native_unit_of_measurement = value.unit
        self._attr_device_class, self._attr_state_class = UNIT_CLASSES.get(value.unit, (None, None))
        if value.reg < 500 or value.key in DIAGNOSTIC_KEYS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return r.decode_value(self._value, self.regs)


class DerivedSensor(DeyeEntity, SensorEntity):
    """A diagnostic sensor whose value is built from the data, not from one register."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DeyeCoordinator, key: str, name: str, read: Decoder) -> None:
        super().__init__(coordinator, key, "sensor", f"{DETAIL_PREFIX}{key}")
        self._attr_name = name
        self._read = read

    @property
    def native_value(self):
        return self._read(self.regs)


class StateSensor(DeyeEntity, SensorEntity):
    """The device state, the alarm / fault words and the link's health."""

    _attr_name = "State"

    def __init__(self, coordinator: DeyeCoordinator) -> None:
        super().__init__(coordinator, "state", "sensor", f"{DETAIL_PREFIX}state")

    @property
    def native_value(self):
        return r.text(r.STATES, self.regs.get(r.DEVICE_STATE_REG))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "alarm_words": [self.regs.get(a) for a in r.ALARM_REGS],
            "poll_failures": self.coordinator.failures,
            "last_error": self.coordinator.last_error or None,
            "writes_allowed": self.coordinator.writes_allowed,
        }

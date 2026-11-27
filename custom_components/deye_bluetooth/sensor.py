"""Sensors: the summary and every decoded register."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DeyeCoordinator
from . import registers as r
from .entity import (
    UNIT_CLASSES,
    DetailEntity,
    DeyeEntity,
    SummarySensor,
    ValueAvailable,
    ValueEntity,
    duplicates,
    summary_sensors,
)


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DeyeCoordinator = entry.runtime_data
    m = coordinator.map
    hidden = duplicates(m)
    entities: list[SensorEntity] = [SummaryValueSensor(coordinator, s) for s in summary_sensors(m)]
    entities += [DetailSensor(coordinator, v, v.key in hidden) for v in m.values]
    entities += [SerialSensor(coordinator), StateSensor(coordinator)]
    entities += [FirmwareSensor(coordinator, part) for part in m.firmware]
    async_add_entities(entities)


DIAGNOSTIC_KEYS = ("relay_status", "relay_generator", "device_state", "device_alarm", "fault_1", "fault_2")


class SummaryValueSensor(ValueAvailable, DeyeEntity, SensorEntity):
    def __init__(self, coordinator: DeyeCoordinator, desc: SummarySensor) -> None:
        super().__init__(coordinator, desc.object_id, "sensor", desc.object_id.removeprefix("deye"))
        self._desc = desc
        # a value from a live register is written every live interval, like the ones marked live
        m = coordinator.map
        self.live = desc.live or (desc.source is not None and m.by_key[desc.source].reg in m.live_regs)
        self.settings = desc.source is not None and m.by_key[desc.source].reg in m.slow_regs
        self._attr_name = desc.name
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class, self._attr_state_class = desc.classes

    @property
    def native_value(self):
        return self._desc.value(self.regs)


class DetailSensor(ValueAvailable, ValueEntity, SensorEntity):
    """sensor.deye_inverter_<key>: one register value as the inverter reports it."""

    def __init__(self, coordinator: DeyeCoordinator, value: r.Value, duplicate: bool) -> None:
        super().__init__(coordinator, value, "sensor")
        self._attr_native_unit_of_measurement = value.unit
        self._attr_device_class, self._attr_state_class = UNIT_CLASSES.get(value.unit, (None, None))
        if value.reg in coordinator.map.slow_regs or value.key in DIAGNOSTIC_KEYS:  # identity and settings
            self._attr_entity_category = EntityCategory.DIAGNOSTIC
        if duplicate:
            self._attr_entity_registry_enabled_default = False


class SerialSensor(DetailEntity, SensorEntity):
    _attr_name = "Serial number"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    settings = True  # identity registers, read on the settings poll

    def __init__(self, coordinator: DeyeCoordinator) -> None:
        super().__init__(coordinator, "serial", "sensor")

    @property
    def native_value(self):
        return r.decode_serial(self.regs)


class FirmwareSensor(DetailEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    settings = True  # identity registers, read on the settings poll

    def __init__(self, coordinator: DeyeCoordinator, part: str) -> None:
        super().__init__(coordinator, f"firmware_{part}", "sensor")
        self._part = part
        self._attr_name = f"Firmware {part}"

    @property
    def native_value(self):
        return r.decode_firmware(self.regs, self._part, self.coordinator.map)


class StateSensor(DetailEntity, SensorEntity):
    """The device state, the alarm / fault words and the link's health.

    Not a settings entity: its attributes change with every poll failure.
    """

    _attr_name = "State"

    def __init__(self, coordinator: DeyeCoordinator) -> None:
        super().__init__(coordinator, "state", "sensor")

    @property
    def native_value(self):
        return r.decode_value(self.coordinator.map.state, self.regs)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "alarm_words": [self.regs.get(a) for a in self.coordinator.map.alarm_regs],
            "poll_failures": self.coordinator.failures,
            "last_error": self.coordinator.last_error or None,
            "writes_allowed": self.coordinator.writes_allowed,
        }

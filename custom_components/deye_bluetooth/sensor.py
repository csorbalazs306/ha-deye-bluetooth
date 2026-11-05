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
from .entity import DETAIL_PREFIX, SUMMARY_SENSORS, UNIT_CLASSES, DeyeEntity, SummarySensor


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: DeyeCoordinator = entry.runtime_data
    entities: list[SensorEntity] = [SummaryValueSensor(coordinator, s) for s in SUMMARY_SENSORS]
    entities += [DetailSensor(coordinator, v) for v in r.VALUES]
    entities += [SerialSensor(coordinator), StateSensor(coordinator), WorkModeSensor(coordinator)]
    entities += [FirmwareSensor(coordinator, part) for part in r.FIRMWARE]
    async_add_entities(entities)


DIAGNOSTIC_KEYS = ("relay_status", "relay_generator", "device_state", "device_alarm", "fault_1", "fault_2")


class SummaryValueSensor(DeyeEntity, SensorEntity):
    def __init__(self, coordinator: DeyeCoordinator, desc: SummarySensor) -> None:
        super().__init__(coordinator, desc.object_id, f"sensor.{desc.object_id}")
        self._desc = desc
        self.live = desc.live
        self._attr_name = desc.name
        self._attr_native_unit_of_measurement = desc.unit
        self._attr_device_class, self._attr_state_class = desc.classes

    @property
    def native_value(self):
        return self._desc.value(self.regs)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class DetailSensor(DeyeEntity, SensorEntity):
    """sensor.deye_inverter_<key>: one register value as the inverter reports it."""

    def __init__(self, coordinator: DeyeCoordinator, value: r.Value) -> None:
        super().__init__(coordinator, value.key, f"sensor.{DETAIL_PREFIX}{value.key}")
        self._value = value
        self._attr_name = value.name
        self._attr_native_unit_of_measurement = value.unit
        self._attr_device_class, self._attr_state_class = UNIT_CLASSES.get(value.unit, (None, None))
        if value.reg < 500 or value.key in DIAGNOSTIC_KEYS:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        return r.decode_value(self._value, self.regs)

    @property
    def available(self) -> bool:
        return super().available and self.native_value is not None


class SerialSensor(DeyeEntity, SensorEntity):
    _attr_name = "Serial number"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DeyeCoordinator) -> None:
        super().__init__(coordinator, "serial", f"sensor.{DETAIL_PREFIX}serial")

    @property
    def native_value(self):
        return r.decode_serial(self.regs)


class FirmwareSensor(DeyeEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DeyeCoordinator, part: str) -> None:
        super().__init__(coordinator, f"firmware_{part}", f"sensor.{DETAIL_PREFIX}firmware_{part}")
        self._part = part
        self._attr_name = f"Firmware {part}"

    @property
    def native_value(self):
        return r.decode_firmware(self.regs, self._part)


class StateSensor(DeyeEntity, SensorEntity):
    """The device state, the alarm / fault words and the link's health."""

    _attr_name = "State"

    def __init__(self, coordinator: DeyeCoordinator) -> None:
        super().__init__(coordinator, "state", f"sensor.{DETAIL_PREFIX}state")

    @property
    def native_value(self):
        return r.decode_value(r.STATE, self.regs)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {
            "alarm_words": [self.regs.get(a) for a in r.ALARM_REGS],
            "poll_failures": self.coordinator.failures,
            "last_error": self.coordinator.last_error or None,
            "writes_allowed": self.coordinator.writes_allowed,
        }


class WorkModeSensor(DeyeEntity, SensorEntity):
    _attr_name = "Work mode name"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: DeyeCoordinator) -> None:
        super().__init__(coordinator, "work_mode_name", f"sensor.{DETAIL_PREFIX}work_mode_name")

    @property
    def native_value(self):
        return r.decode_value(r.WORK_MODE_NAME, self.regs)

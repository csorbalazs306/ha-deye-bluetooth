"""Deye Bluetooth: a Deye hybrid inverter over its Wi-Fi/BLE logger, no cloud.

The logger (DYDA_WiBLE) has no local network port, but its Bluetooth console
passes Modbus RTU through to the inverter (see protocol.py).  A Bluetooth adapter
on the Home Assistant host (or an ESPHome Bluetooth proxy near the inverter) holds
the link; this polls the register blocks of registers.py.

Entities (the prefix is ``deye`` for the first logger, ``deye_2`` for the second, ...):
- the summary: ``sensor.deye_grid_power``, ``sensor.deye_grid_phase_*``,
  ``binary_sensor.deye_grid``, ``switch.deye_grid_charge``, ...;
- every decoded register as ``sensor.deye_inverter_<key>``;
- the settings of registers.py's NUMBERS, SELECTS, TIMES and ON_OFF_SWITCHES as
  controls on the device page, which write only when someone changes them.

The grid phases and the load are read every second (live_interval: the logger
answers in ~0.3 s and the values change about every second), everything else
every 15 s, the settings every 5 min (all on the options page, like every limit
the inverter does not report itself: options.py).

Writes (the controls, the admin-only ``deye_bluetooth.write_register`` action) are
off until the "Allow writes" option is on; each written register is read back and a
mismatch is an error.  Only changed registers are written (EEPROM).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import cached_property
from math import inf
from time import monotonic  # not "import time": this package's time.py would replace it

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.service import async_register_admin_service
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from . import options as o
from . import registers as r
from .protocol import MODBUS_MAX_READ
from .transport import Link, LinkError, RegisterRejected

DOMAIN = "deye_bluetooth"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.SELECT, Platform.TIME]
CONF_ADDRESS = "address"
CONF_PREFIX = "entity_prefix"
CONF_ENTRY = "config_entry_id"

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
WRITE_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY): cv.string,
        vol.Required("address"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
        vol.Required("value"): vol.Any(
            vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
            vol.All(cv.ensure_list, [vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF))], vol.Length(min=1, max=16)),
        ),
    }
)
READ_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_ENTRY): cv.string,
        vol.Required("address"): vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF)),
        vol.Optional("count", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=MODBUS_MAX_READ)),
    }
)


class DeyeCoordinator(DataUpdateCoordinator[dict[int, int]]):
    """The data: {register: word} of every fresh block."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN,
            update_interval=timedelta(seconds=entry.options.get(o.LIVE_S, o.DEFAULTS[o.LIVE_S])),
        )
        self.address: str = entry.data[CONF_ADDRESS]
        self.prefix: str = entry.data[CONF_PREFIX]
        self.link = Link(
            slave=self.option(o.SLAVE), max_read=self.option(o.MAX_READ), write_char=self.option(o.WRITE_CHAR),
            notify_char=self.option(o.NOTIFY_CHAR), reply_timeout=self.option(o.REPLY_TIMEOUT),
            connect_attempts=self.option(o.CONNECT_ATTEMPTS),
        )
        self.lock = asyncio.Lock()
        self.blocks: dict[int, tuple[float, list[int]]] = {}  # start -> (monotonic, words)
        # never read: monotonic() is the host's uptime, which can be below any interval
        self._fast_at = -inf
        self._slow_at = -inf
        self.full_poll = True  # this update read more than the live blocks
        self.failures = 0
        self.last_error = ""

    def option(self, key: str):
        """What the options page says, or its default."""
        return self.config_entry.options.get(key, o.DEFAULTS[key])

    @property
    def writes_allowed(self) -> bool:
        return bool(self.option(o.WRITES))

    @cached_property
    def device_info(self) -> DeviceInfo:
        """The device page of this logger: what the inverter says about itself."""
        data = self.registers()
        return DeviceInfo(
            identifiers={(DOMAIN, self.config_entry.unique_id)},
            name=f"Deye inverter{self.prefix.removeprefix('deye').replace('_', ' ')}",
            manufacturer="Deye",
            model=r.device_model(data),
            serial_number=r.decode_serial(data),
        )

    async def _ensure_link(self) -> None:
        if self.link.connected:
            return
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise LinkError(f"{self.address} not in range of a connectable Bluetooth adapter")
        await self.link.connect(device)

    async def _read_blocks(self, blocks: list[tuple[int, int]]) -> None:
        for start, count in blocks:
            try:
                words = await self.link.read(start, count)
            except RegisterRejected as exc:  # its values stay unavailable, the rest goes on
                _LOGGER.debug("Deye Bluetooth: registers %d+%d: %s", start, count, exc)
                continue
            self.blocks[start] = (monotonic(), words)

    async def _poll(self, blocks: list[tuple[int, int]], last: float, interval: str) -> tuple[float, bool]:
        """Read a group when its interval passed: the stamp to keep, and whether it read."""
        if monotonic() - last < self.option(interval):
            return last, False
        await self._read_blocks(blocks)
        return monotonic(), True

    async def _async_update_data(self) -> dict[int, int]:
        async with self.lock:
            self.full_poll = False
            try:
                await self._ensure_link()
                await self._read_blocks(r.LIVE)
                self._fast_at, self.full_poll = await self._poll(r.FAST, self._fast_at, o.SCAN_S)
                self._slow_at, _ = await self._poll(r.SLOW, self._slow_at, o.SETTINGS_S)
                self.failures, self.last_error = 0, ""
            except LinkError as exc:
                self.failures += 1
                self.last_error = str(exc)
                await self.link.close()
                _LOGGER.debug("Deye BLE poll failed (%d in a row): %s", self.failures, exc)
        data = self.registers()
        if not any(start in self.blocks and self._fresh(start) for start, _ in r.LIVE + r.FAST):
            raise UpdateFailed(f"no fresh data ({self.last_error or 'no reply'})")
        return data

    def _fresh(self, start: int) -> bool:
        stale = self.option(o.SETTINGS_STALE_S) if start in r.SLOW_STARTS else self.option(o.STALE_S)
        at, _ = self.blocks[start]
        return monotonic() - at < stale

    def registers(self) -> dict[int, int]:
        """The words of the fresh blocks only: a stale value is no value."""
        out: dict[int, int] = {}
        for start, (_, words) in self.blocks.items():
            if self._fresh(start):
                out.update({start + i: w for i, w in enumerate(words)})
        return out

    async def read(self, address: int, count: int) -> list[int]:
        """Any registers, on demand (the read_registers action: finding a value)."""
        async with self.lock:
            try:
                await self._ensure_link()
                return await self.link.read(address, count)
            except LinkError as exc:
                await self.link.close()
                raise HomeAssistantError(f"Deye Bluetooth: read of {address} failed: {exc}") from exc

    async def write(self, address: int, values: list[int]) -> None:
        """Write consecutive registers, each read back; unchanged ones are skipped."""
        if not self.writes_allowed:
            raise HomeAssistantError("Deye Bluetooth: writes are off (integration options → Allow writes)")
        async with self.lock:
            try:
                await self._ensure_link()
                current = await self.link.read(address, len(values))
                for i, value in enumerate(values):
                    if current[i] != value:
                        await self._write_verified(address + i, value)
                self._fast_at = self._slow_at = -inf  # read everything again on the next poll
            except LinkError as exc:
                await self.link.close()
                raise HomeAssistantError(f"Deye Bluetooth: write to {address} failed: {exc}") from exc
        _LOGGER.info("Deye Bluetooth: wrote %s to register %d", values, address)
        await self.async_request_refresh()

    async def _write_verified(self, address: int, value: int) -> None:
        attempts = self.option(o.WRITE_ATTEMPTS)
        for attempt in range(1, attempts + 1):
            try:
                await self._ensure_link()
                await self.link.write(address, value)
                (shown,) = await self.link.read(address, 1)
            except LinkError as exc:
                await self.link.close()
                if attempt == attempts:
                    raise
                _LOGGER.debug("Deye Bluetooth: write %d=%d attempt %d: %s", address, value, attempt, exc)
                await asyncio.sleep(2.0 * attempt)
                continue
            if shown != value:
                raise LinkError(f"register {address} reads {shown} after writing {value}")
            return


@callback
def _coordinator(hass: HomeAssistant, call: ServiceCall) -> DeyeCoordinator:
    """The logger the action names; it may be left out while there is only one."""
    entries = hass.config_entries.async_loaded_entries(DOMAIN)
    wanted = call.data.get(CONF_ENTRY)
    if wanted is not None:
        entries = [entry for entry in entries if entry.entry_id == wanted]
        if not entries:
            raise ServiceValidationError(f"Deye Bluetooth: no loaded logger {wanted}")
    if not entries:
        raise ServiceValidationError("Deye Bluetooth: no logger is loaded")
    if len(entries) > 1:
        raise ServiceValidationError(f"Deye Bluetooth: several loggers, set {CONF_ENTRY}")
    return entries[0].runtime_data


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def write_register(call: ServiceCall) -> None:
        value = call.data["value"]
        await _coordinator(hass, call).write(call.data["address"], value if isinstance(value, list) else [value])

    async def read_registers(call: ServiceCall) -> ServiceResponse:
        start, count = call.data["address"], call.data["count"]
        return {"address": start, "values": await _coordinator(hass, call).read(start, count)}

    async_register_admin_service(hass, DOMAIN, "write_register", write_register, schema=WRITE_SCHEMA)
    hass.services.async_register(DOMAIN, "read_registers", read_registers, schema=READ_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = DeyeCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    device_type = coordinator.registers().get(r.DEVICE_TYPE_REG)
    if device_type is not None and device_type not in r.VERIFIED_DEVICE_TYPES:
        _LOGGER.warning(
            "Deye Bluetooth: device type 0x%X is not verified, its register map may differ; "
            "keep writes off until the values look right", device_type,
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_reload))
    return True


async def _reload(hass: HomeAssistant, entry: ConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.link.close()
    return ok

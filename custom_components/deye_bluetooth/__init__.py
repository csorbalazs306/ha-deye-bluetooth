"""Deye Bluetooth: a Deye hybrid inverter over its Wi-Fi/BLE logger, no cloud.

The logger (DYDA_WiBLE) has no local network port, but its Bluetooth console
passes Modbus RTU through to the inverter (see protocol.py).  A Bluetooth adapter
on the Home Assistant host (or an ESPHome Bluetooth proxy near the inverter) holds
the link; this polls the register blocks of registers.py.

Entities (the prefix is ``deye`` for the first logger, ``deye_2`` for the second, ...):
- the summary: ``sensor.deye_grid_power``, ``sensor.deye_grid_phase_*``,
  ``binary_sensor.deye_grid``, ``switch.deye_grid_charge``, ...;
- every decoded register as ``sensor.deye_inverter_<key>``; those that repeat a control
  or a summary sensor are disabled by default;
- the settings of registers.py's NUMBERS, SELECTS, TIMES and ON_OFF_SWITCHES as
  controls on the device page, which write only when someone changes them.

The grid currents, powers and the load are read every second (live_interval: the logger
answers in ~0.3 s and the values change about every second), the grid voltages and
everything else every 15 s, the settings every 5 min.  Only the intervals, write safety and the
Bluetooth link are on the options page (options.py); the inverter's own settings
are controls on the device page.

Writes (the controls, the admin-only ``deye_bluetooth.write_register`` action) are
off until the "Allow writes" option is on; each written register is read back and a
mismatch is an error.  Only changed registers are written (EEPROM).  The next poll
reads again the poll blocks a write touched.  After a failed connection the polls wait
2, 4, ... up to 60 s before they connect again; the read and write actions do not wait.

When the logger has not answered for the "Alert after" option, a Repairs issue says so.  The
logger has no pairing or password and takes one connection at a time, so a long silence can
also mean that another device in Bluetooth range holds it.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import timedelta
from math import inf
from time import monotonic  # not "import time": this package's time.py would replace it

import voluptuous as vol
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_CONFIG_ENTRY_ID, CONF_ADDRESS, Platform
from homeassistant.core import HomeAssistant, ServiceCall, ServiceResponse, SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.service import async_get_config_entry, async_register_admin_service
from homeassistant.helpers.typing import ConfigType
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.hass_dict import HassKey

from . import options as o
from . import registers as r
from .protocol import MODBUS_MAX_READ, LinkError, RegisterRejected
from .transport import Link

DOMAIN = "deye_bluetooth"
PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.SELECT, Platform.TIME]
CONF_PREFIX = "entity_prefix"

_LOGGER = logging.getLogger(__name__)

MAX_BACKOFF_S = 60  # the wait after a failed connection is 2 ** failures seconds, at most this
# entry_id -> monotonic of the first poll the logger did not answer.  In hass.data, not on the coordinator: a setup
# retry builds a new coordinator, and a logger that never answers must still raise the issue.
DOWN_SINCE: HassKey[dict[str, float]] = HassKey(f"{DOMAIN}_down_since")
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_WORD_VALUE = vol.All(vol.Coerce(int), vol.Range(min=0, max=0xFFFF))
WRITE_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required("address"): _WORD_VALUE,
        vol.Required("value"): vol.Any(
            _WORD_VALUE,
            vol.All(cv.ensure_list, [_WORD_VALUE], vol.Length(min=1, max=16)),
        ),
    }
)
READ_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required("address"): _WORD_VALUE,
        vol.Optional("count", default=1): vol.All(vol.Coerce(int), vol.Range(min=1, max=MODBUS_MAX_READ)),
    }
)


@dataclass(frozen=True, eq=False)  # by identity: each group is a key of the read times
class PollGroup:
    """Poll blocks read together: on every update (interval_key None) or on an option's interval.

    stale_key is the option for how long their values stay fresh.
    """

    blocks: list[tuple[int, int]]
    interval_key: str | None
    stale_key: str


POLL_GROUPS = (
    PollGroup(r.LIVE, None, o.STALE_S),
    PollGroup(r.FAST, o.SCAN_S, o.STALE_S),
    PollGroup(r.SLOW, o.SETTINGS_S, o.SETTINGS_STALE_S),
)
LIVE_GROUP, FAST_GROUP, SLOW_GROUP = POLL_GROUPS
_BLOCKS = {start: (count, group) for group in POLL_GROUPS for start, count in group.blocks}  # start -> (count, group)


class _OutOfRange(LinkError):
    """The logger is not in range: nothing was tried, so there is no backoff and the next poll looks again."""


def _blocks_at(register: int) -> list[int]:
    """The starts of the poll blocks that read this register."""
    return [start for start, (count, _) in _BLOCKS.items() if start <= register < start + count]


def _issue_id(entry_id: str) -> str:
    return f"link_down_{entry_id}"


def _forget_link(hass: HomeAssistant, entry_id: str) -> None:
    """No alert for an entry that does not poll: unloaded or removed."""
    hass.data.get(DOWN_SINCE, {}).pop(entry_id, None)
    ir.async_delete_issue(hass, DOMAIN, _issue_id(entry_id))


class DeyeCoordinator(DataUpdateCoordinator[dict[int, int]]):
    """The data: {register: word} of every fresh block."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self._options = {**o.DEFAULTS, **entry.options}  # any change of the options reloads the entry
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=DOMAIN,
            update_interval=timedelta(seconds=self._options[o.LIVE_S]),
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
        self.fresh_blocks: frozenset[int] = frozenset()  # the starts of the blocks fresh at the last update
        # never read: monotonic() is the host's uptime, which can be below any interval
        self._read_at = {group: -inf for group in POLL_GROUPS}
        self._pending: set[int] = set()  # blocks a write touched: read on the next poll
        self._retry_at = -inf  # no poll connects before this, after a failed connection
        self.full_poll = True  # the last update read a FAST block
        self.slow_poll = True  # the last update read a SLOW block
        self.failures = 0
        self._link_failures = 0  # the backoff's exponent: out-of-range polls never tried to connect, so they do not count
        self.last_error = ""

    def option(self, key: str):
        """What the options page says, or its default."""
        return self._options[key]

    @property
    def writes_allowed(self) -> bool:
        return bool(self.option(o.WRITES))

    def _device(self):
        device = bluetooth.async_ble_device_from_address(self.hass, self.address, connectable=True)
        if device is None:
            raise _OutOfRange(f"{self.address} not in range of a connectable Bluetooth adapter")
        return device

    async def _connect(self, device) -> None:
        await self.link.connect(device)
        if self.link.found_console is not None:
            self._remember_console(*self.link.found_console)

    def _remember_console(self, write_char: str, notify_char: str) -> None:
        """Keep the pair a scan found in the entry, so the next start does not scan again.

        async_update_entry does not reload the entry: only the options flow does.
        """
        if (write_char, notify_char) == (self._options[o.WRITE_CHAR], self._options[o.NOTIFY_CHAR]):
            return
        self._options[o.WRITE_CHAR], self._options[o.NOTIFY_CHAR] = write_char, notify_char
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            options={**self.config_entry.options, o.WRITE_CHAR: write_char, o.NOTIFY_CHAR: notify_char},
        )

    async def _ensure_link(self) -> None:
        """For the read and write actions: connect if needed, whatever the backoff says."""
        if not self.link.connected:
            await self._connect(self._device())

    async def _poll(self) -> bool:
        """Connect if needed, then read the groups that are due and the blocks writes left pending.

        False when the reconnect is still backed off: nothing was read.
        """
        if not self.link.connected:
            device = self._device()  # out of range: no backoff, the next poll looks again
            if monotonic() < self._retry_at:
                return False
            await self._connect(device)
        now = monotonic()
        due = [g for g in POLL_GROUPS if g.interval_key is None or now - self._read_at[g] >= self.option(g.interval_key)]
        blocks = [
            (start, count) for group in POLL_GROUPS for start, count in group.blocks
            if group in due or start in self._pending
        ]
        await self._read_blocks(blocks)
        now = monotonic()
        for group in due:
            self._read_at[group] = now
        return True

    async def _read_blocks(self, blocks: list[tuple[int, int]]) -> None:
        for start, count in blocks:
            try:
                words = await self.link.read(start, count)
            except RegisterRejected as exc:  # its values age out, the rest goes on
                _LOGGER.debug("Deye Bluetooth: registers %d+%d: %s", start, count, exc)
            else:
                self.blocks[start] = (monotonic(), words)
            self._pending.discard(start)  # answered, even by a refusal: reading it again now would not change that
            group = _BLOCKS[start][1]
            if group is FAST_GROUP:
                self.full_poll = True
            elif group is SLOW_GROUP:
                self.slow_poll = True

    async def _async_update_data(self) -> dict[int, int]:
        async with self.lock:
            self.full_poll = self.slow_poll = False
            polled = False
            try:
                polled = await self._poll()
            except LinkError as exc:
                self.failures += 1
                self.last_error = str(exc)
                await self.link.close()
                if not isinstance(exc, _OutOfRange):
                    self._link_failures += 1
                    self._retry_at = monotonic() + min(2 ** min(self._link_failures, 6), MAX_BACKOFF_S)
                _LOGGER.debug("Deye BLE poll failed (%d in a row): %s", self.failures, exc)
            else:
                if polled:
                    self.failures, self.last_error, self._retry_at = 0, "", -inf
                    self._link_failures = 0
            self._watch_link(polled)
        self.fresh_blocks = frozenset(start for start in self.blocks if self._fresh(start))
        data = self.registers()
        if not any(start in self.fresh_blocks for start, _ in LIVE_GROUP.blocks + FAST_GROUP.blocks):
            raise UpdateFailed(f"no fresh data ({self.last_error or 'no reply'})")
        return data

    def _watch_link(self, answered: bool) -> None:
        """The Repairs issue while the logger has not answered for the alert option (0: never); gone once it does.

        A poll waiting out its backoff did not reach the logger either: it counts as unanswered.
        """
        entry = self.config_entry
        down = self.hass.data.setdefault(DOWN_SINCE, {})
        if answered:
            if down.pop(entry.entry_id, None) is not None:
                ir.async_delete_issue(self.hass, DOMAIN, _issue_id(entry.entry_id))
            return
        since = down.setdefault(entry.entry_id, monotonic())
        after = self.option(o.ALERT_S)
        if after and monotonic() - since >= after:
            ir.async_create_issue(  # the same arguments again change nothing: no event, no save
                self.hass, DOMAIN, _issue_id(entry.entry_id),
                is_fixable=False, severity=ir.IssueSeverity.WARNING, translation_key="link_down",
                translation_placeholders={"name": entry.title, "minutes": f"{round(after / 60, 1):g}"},
            )

    def _fresh(self, start: int) -> bool:
        at, _ = self.blocks[start]
        return monotonic() - at < self.option(_BLOCKS[start][1].stale_key)

    def registers(self) -> dict[int, int]:
        """The words of the fresh blocks only: a stale value is no value."""
        out: dict[int, int] = {}
        for start, (_, words) in self.blocks.items():
            if self._fresh(start):
                out.update({start + i: w for i, w in enumerate(words)})
        return out

    @asynccontextmanager
    async def _session(self, what: str) -> AsyncIterator[None]:
        """The lock and a connected link for one read or write action.

        A broken link is closed, so the next action connects again.  A refused register is an answer from the
        inverter: the link stays up and the action fails.
        """
        async with self.lock:
            try:
                await self._ensure_link()
                yield
            except LinkError as exc:
                await self.link.close()
                raise HomeAssistantError(f"Deye Bluetooth: {what} failed: {exc}") from exc
            except RegisterRejected as exc:
                raise HomeAssistantError(f"Deye Bluetooth: {what} failed: {exc}") from exc

    async def read(self, address: int, count: int) -> list[int]:
        """Any registers, on demand (the read_registers action: finding a value)."""
        async with self._session(f"read of {address}"):
            return await self.link.read(address, count)

    async def write(self, address: int, values: list[int], *, refresh: bool = True) -> None:
        """Write consecutive registers, each read back; unchanged ones are skipped.

        The poll blocks that were written are read on the next poll.  That poll is requested now unless refresh is
        False: the caller then requests it once, after several writes.
        """
        if not self.writes_allowed:
            raise HomeAssistantError("Deye Bluetooth: writes are off (integration options → Allow writes)")
        written = False
        async with self._session(f"write to {address}"):
            current = await self.link.read(address, len(values))
            for i, value in enumerate(values):
                if current[i] == value:
                    continue
                try:
                    await self._write_verified(address + i, value)
                finally:  # attempted, so possibly changed even if it failed: its block is read again
                    self._pending.update(_blocks_at(address + i))
                written = True
        if written:
            _LOGGER.info("Deye Bluetooth: wrote %s to register %d", values, address)
        if refresh:
            await self.async_request_refresh()

    async def _write_verified(self, address: int, value: int) -> None:
        """One register, written and read back.  A link error is retried after a wait; a refusal is not."""
        attempts = self.option(o.WRITE_ATTEMPTS)
        for attempt in range(1, attempts + 1):
            try:
                await self._ensure_link()
                await self.link.write(address, value)
                (shown,) = await self.link.read(address, 1)
            except LinkError as exc:
                if attempt == attempts:
                    raise  # the caller closes the link
                await self.link.close()
                _LOGGER.debug("Deye Bluetooth: write %d=%d attempt %d: %s", address, value, attempt, exc)
                await asyncio.sleep(2.0 * attempt)
                continue
            if shown != value:
                raise LinkError(f"register {address} reads {shown} after writing {value}")
            return


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """1.2.0 moved the inverter/battery limits to the device page: drop their stale keys."""
    if entry.version < 2:
        options = {key: value for key, value in entry.options.items() if key in o.DEFAULTS}
        hass.config_entries.async_update_entry(entry, options=options, version=2)
    return True


def _check_range(start: int, count: int) -> None:
    """A range past the last address cannot be sent: refused before the link is used."""
    if start + count > 0x10000:
        raise ServiceValidationError(f"registers {start} to {start + count - 1} run past the last address, 65535")


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    async def write_register(call: ServiceCall) -> None:
        value = call.data["value"]
        values = value if isinstance(value, list) else [value]
        _check_range(call.data["address"], len(values))
        coordinator = async_get_config_entry(hass, DOMAIN, call.data.get(ATTR_CONFIG_ENTRY_ID)).runtime_data
        await coordinator.write(call.data["address"], values)

    async def read_registers(call: ServiceCall) -> ServiceResponse:
        start, count = call.data["address"], call.data["count"]
        _check_range(start, count)
        coordinator = async_get_config_entry(hass, DOMAIN, call.data.get(ATTR_CONFIG_ENTRY_ID)).runtime_data
        return {"address": start, "values": await coordinator.read(start, count)}

    async_register_admin_service(hass, DOMAIN, "write_register", write_register, schema=WRITE_SCHEMA)
    hass.services.async_register(DOMAIN, "read_registers", read_registers, schema=READ_SCHEMA,
                                 supports_response=SupportsResponse.ONLY)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    coordinator = DeyeCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    device_type = coordinator.data.get(r.DEVICE_TYPE_REG)
    if device_type is not None and device_type not in r.VERIFIED_DEVICE_TYPES:
        _LOGGER.warning(
            "Deye Bluetooth: device type 0x%X is not verified, its register map may differ; "
            "keep writes off until the values look right", device_type,
        )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.link.close()
        _forget_link(hass, entry.entry_id)
    return ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Also for an entry removed while its setup was retrying, which is not unloaded."""
    _forget_link(hass, entry.entry_id)

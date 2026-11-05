"""Shared by the integration tests: the fake logger link and the helpers that set the integration up."""
from contextlib import contextmanager
from math import inf
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.entity import Entity
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_bluetooth import DOMAIN
from custom_components.deye_bluetooth import options as o
from custom_components.deye_bluetooth import protocol as p
from custom_components.deye_bluetooth.transport import LinkError

WRITE_UUID = "11111111-2222-3333-4444-555555555555"
NOTIFY_UUID = "66666666-7777-8888-9999-aaaaaaaaaaaa"
ADDRESS, ADDRESS_2 = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
SERIAL = {3: 12594, 4: 13108, 5: 13622, 6: 14136, 7: 14640}


class FakeLink:
    """Answers reads from a register dict, keeps writes in it.  The tests switch the failures on."""

    scan_result: tuple[str, str] | None = None  # the pair a scan of this logger finds, as Link.found_console

    def __init__(self, **settings) -> None:
        self.settings = settings
        self.regs = {0: 5, 20: 50000, 21: 0, 117: 10, 607: 32, 598: 2300, 599: 2300, 600: 2300, 552: 5, 145: 0, **SERIAL}
        self.connected = False
        self.connects = 0
        self.found_console: tuple[str, str] | None = None
        self.writes: list[tuple[int, int]] = []
        self.write_calls = 0  # every write sent, the refused ones too
        self.reads: list[tuple[int, int]] = []
        self.fail_connect = False
        self.fail_reads = False
        self.fail_write_at: int | None = None  # a write to this register is lost: a link error
        self.rejected: set[int] = set()  # reads of the block starting here are refused by the inverter
        self.refused_writes: set[int] = set()  # writes to these registers are refused by the inverter

    async def connect(self, device) -> None:
        self.connects += 1
        if self.fail_connect:
            raise LinkError("connect failed (test)")
        self.connected = True
        self.found_console = FakeLink.scan_result

    async def close(self) -> None:
        self.connected = False

    async def read(self, address: int, count: int) -> list[int]:
        self.reads.append((address, count))
        if self.fail_reads:
            raise LinkError("no reply (test)")
        if address in self.rejected:
            raise p.RegisterRejected("Modbus exception 0x02 (function 0x03)")
        return [self.regs.get(address + i, 0) for i in range(count)]

    async def write(self, address: int, value: int) -> None:
        self.write_calls += 1
        if address == self.fail_write_at:
            raise LinkError("write dropped (test)")
        if address in self.refused_writes:
            raise p.RegisterRejected("Modbus exception 0x02 (function 0x10)")
        self.writes.append((address, value))
        self.regs[address] = value


async def _add(hass: HomeAssistant, address=ADDRESS, prefix="deye", writes=False, **options) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=address, data={"address": address, "entity_prefix": prefix},
        options={"allow_writes": writes, **options},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def _refresh(hass: HomeAssistant, coordinator) -> None:
    await coordinator.async_refresh()
    await hass.async_block_till_done()


def _due(coordinator, *groups) -> None:
    """Make these poll groups due at the next update."""
    for group in groups:
        coordinator._read_at[group] = -inf


@contextmanager
def _written():
    """The entity ids whose state was written to Home Assistant, in order."""
    written: list[str] = []
    original = Entity.async_write_ha_state

    def record(self) -> None:
        written.append(self.entity_id)
        original(self)

    with patch.object(Entity, "async_write_ha_state", record):
        yield written


def _age(coordinator, start: int, seconds: float) -> None:
    """The block at start was read this many seconds ago (its values go stale when that exceeds the option)."""
    at, words = coordinator.blocks[start]
    coordinator.blocks[start] = (at - seconds, words)


async def _options(hass: HomeAssistant, entry: MockConfigEntry, **changes):
    """Submit the options page with these values changed, the rest as shown."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert set(changes) <= {opt.key for opt in o.OPTIONS}, f"unknown option(s): {set(changes) - {opt.key for opt in o.OPTIONS}}"
    values = {opt.section: {} for opt in o.OPTIONS}
    for opt in o.OPTIONS:
        values[opt.section][opt.key] = changes.get(opt.key, entry.options.get(opt.key, opt.default))
    return await hass.config_entries.options.async_configure(result["flow_id"], values)

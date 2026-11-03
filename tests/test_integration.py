"""The integration inside a real Home Assistant, with the Bluetooth link faked."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_bluetooth import DOMAIN
from custom_components.deye_bluetooth.diagnostics import async_get_config_entry_diagnostics

ADDRESS, ADDRESS_2 = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
SERIAL = {3: 12594, 4: 13108, 5: 13622, 6: 14136, 7: 14640}


class FakeLink:
    """Answers reads from a register dict, keeps writes in it."""

    def __init__(self) -> None:
        self.regs = {0: 5, 20: 50000, 21: 0, 117: 10, 607: 32, 598: 2300, 599: 2300, 600: 2300, 552: 5, 145: 0, **SERIAL}
        self.connected = False
        self.writes: list[tuple[int, int]] = []

    async def connect(self, device) -> None:
        self.connected = True

    async def close(self) -> None:
        self.connected = False

    async def read(self, address: int, count: int) -> list[int]:
        return [self.regs.get(address + i, 0) for i in range(count)]

    async def write(self, address: int, value: int) -> None:
        self.writes.append((address, value))
        self.regs[address] = value


@pytest.fixture(autouse=True)
def _harness(enable_custom_integrations):
    links: list[FakeLink] = []

    def make_link() -> FakeLink:
        links.append(FakeLink())
        return links[-1]

    with (
        patch("custom_components.deye_bluetooth.Link", side_effect=make_link),
        patch("custom_components.deye_bluetooth.bluetooth.async_ble_device_from_address", return_value=MagicMock()),
        patch("homeassistant.setup.async_process_deps_reqs", new=AsyncMock()),
        patch("homeassistant.setup._async_process_dependencies", new=AsyncMock(return_value=[])),
    ):
        yield links


async def _add(hass: HomeAssistant, address=ADDRESS, prefix="deye", writes=False) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=address, data={"address": address, "entity_prefix": prefix},
        options={"allow_writes": writes},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


async def test_entities_and_model(hass: HomeAssistant) -> None:
    await _add(hass)
    assert hass.states.get("sensor.deye_grid_power").state == "32"
    assert hass.states.get("binary_sensor.deye_grid").state == "on"
    assert hass.states.get("sensor.deye_inverter_serial").state == "1234567890"
    assert hass.states.get("number.deye_inverter_max_sell_power").attributes["max"] == 5000


async def test_two_loggers(hass: HomeAssistant) -> None:
    first = await _add(hass)
    second = await _add(hass, ADDRESS_2, "deye_2", writes=True)
    assert hass.states.get("sensor.deye_grid_power") is not None
    assert hass.states.get("sensor.deye_2_grid_power") is not None
    assert hass.states.get("sensor.deye_2_inverter_serial") is not None

    with pytest.raises(ServiceValidationError, match="several loggers"):
        await hass.services.async_call(DOMAIN, "write_register", {"address": 145, "value": 1}, blocking=True)
    await hass.services.async_call(
        DOMAIN, "write_register", {"address": 145, "value": 1, "config_entry_id": second.entry_id}, blocking=True
    )
    assert second.runtime_data.link.writes == [(145, 1)]
    assert first.runtime_data.link.writes == []

    await hass.config_entries.async_unload(first.entry_id)
    await hass.services.async_call(DOMAIN, "write_register", {"address": 145, "value": 0}, blocking=True)  # one is left
    assert second.runtime_data.link.writes[-1] == (145, 0)


async def test_writes_are_off_by_default(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    with pytest.raises(HomeAssistantError, match="writes are off"):
        await hass.services.async_call(DOMAIN, "write_register", {"address": 145, "value": 1}, blocking=True)
    assert entry.runtime_data.link.writes == []


async def test_grid_charge_switch_writes_and_reads_back(hass: HomeAssistant) -> None:
    entry = await _add(hass, writes=True)
    await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.deye_grid_charge"}, blocking=True)
    await hass.async_block_till_done()
    link = entry.runtime_data.link
    assert (130, 1) in link.writes and (172, 1) in link.writes and (166, 100) in link.writes
    await entry.runtime_data.async_refresh()  # the next poll
    await hass.async_block_till_done()
    assert hass.states.get("switch.deye_grid_charge").state == "on"


async def test_read_registers_action(hass: HomeAssistant) -> None:
    await _add(hass)
    result = await hass.services.async_call(
        DOMAIN, "read_registers", {"address": 607, "count": 1}, blocking=True, return_response=True
    )
    assert result == {"address": 607, "values": [32]}


async def test_unknown_model_warns(hass: HomeAssistant, caplog, _harness) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={"address": ADDRESS, "entity_prefix": "deye"})
    entry.add_to_hass(hass)
    with patch.object(FakeLink, "read", new=AsyncMock(side_effect=lambda a, n: [3 if a == 0 else 0] * n)):
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
    assert "is not verified" in caplog.text


async def test_diagnostics_leave_out_serial_and_address(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    entry.runtime_data.last_error = f"{ADDRESS} not in range"
    data = await async_get_config_entry_diagnostics(hass, entry)
    text = str(data)
    assert ADDRESS not in text
    assert 3 not in data["registers"] and 7 not in data["registers"]
    assert data["registers"][607] == 32


async def test_flow_gives_the_second_logger_its_own_prefix(hass: HomeAssistant) -> None:
    await _add(hass)
    info = MagicMock(address=ADDRESS_2, service_uuids=["00000922-0000-1000-8000-00805f9b34fb"])
    info.name = "logger2"
    with patch("custom_components.deye_bluetooth.config_flow.async_discovered_service_info", return_value=[info]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDRESS_2})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"address": ADDRESS_2, "entity_prefix": "deye_2"}


async def test_flow_without_a_logger_aborts(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    with patch("custom_components.deye_bluetooth.config_flow.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"

"""The integration inside a real Home Assistant, with the Bluetooth link faked."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_bluetooth import DOMAIN
from custom_components.deye_bluetooth import options as o
from custom_components.deye_bluetooth import protocol as p
from custom_components.deye_bluetooth.diagnostics import async_get_config_entry_diagnostics
from custom_components.deye_bluetooth.transport import Link

WRITE_UUID = "11111111-2222-3333-4444-555555555555"
ADDRESS, ADDRESS_2 = "AA:BB:CC:DD:EE:01", "AA:BB:CC:DD:EE:02"
SERIAL = {3: 12594, 4: 13108, 5: 13622, 6: 14136, 7: 14640}


class FakeLink:
    """Answers reads from a register dict, keeps writes in it."""

    def __init__(self, **settings) -> None:
        self.settings = settings
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

    def make_link(**settings) -> FakeLink:
        links.append(FakeLink(**settings))
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


async def test_first_poll_reads_everything_right_after_a_host_reboot(hass: HomeAssistant) -> None:
    with patch("custom_components.deye_bluetooth.monotonic", return_value=30.0):
        await _add(hass)
    assert hass.states.get("sensor.deye_inverter_serial").state == "1234567890"


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


async def test_write_register_is_for_admins_only(hass: HomeAssistant, hass_read_only_user, hass_admin_user) -> None:
    entry = await _add(hass, writes=True)
    call = (DOMAIN, "write_register", {"address": 145, "value": 1})
    with pytest.raises(Unauthorized):
        await hass.services.async_call(*call, blocking=True, context=Context(user_id=hass_read_only_user.id))
    assert entry.runtime_data.link.writes == []
    await hass.services.async_call(*call, blocking=True, context=Context(user_id=hass_admin_user.id))
    assert entry.runtime_data.link.writes == [(145, 1)]


async def test_grid_charge_switch_writes_only_the_flags_and_the_enable(hass: HomeAssistant) -> None:
    entry = await _add(hass, writes=True)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    socs = {166 + i: 30 + 10 * i for i in range(6)}  # someone's own schedule
    link.regs |= socs | {172: 2, 173: 3}  # two programs with a generator flag
    coordinator._slow_at = float("-inf")
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    async def flip(service: str) -> list[tuple[int, int]]:
        link.writes.clear()
        await hass.services.async_call("switch", service, {"entity_id": "switch.deye_grid_charge"}, blocking=True)
        await coordinator.async_refresh()  # the next poll
        await hass.async_block_till_done()
        return sorted(link.writes)

    assert await flip("turn_on") == [(130, 1), (172, 3), (174, 1), (175, 1), (176, 1), (177, 1)]
    assert hass.states.get("switch.deye_grid_charge").state == "on"
    assert await flip("turn_off") == [(130, 0), (172, 2), (173, 2), (174, 0), (175, 0), (176, 0), (177, 0)]
    assert hass.states.get("switch.deye_grid_charge").state == "off"
    assert {a: link.regs[a] for a in socs} == socs
    assert hass.states.get("number.deye_inverter_program_1_soc").state == "30"


async def test_the_dangerous_settings_write_like_the_others(hass: HomeAssistant) -> None:
    entry = await _add(hass, writes=True)
    link = entry.runtime_data.link

    async def number(key: str, value: float) -> None:
        await hass.services.async_call(
            "number", "set_value", {"entity_id": f"number.deye_inverter_{key}", "value": value}, blocking=True
        )

    await number("grid_protection_voltage_high", 253.5)
    await number("grid_protection_frequency_low", 47.5)
    await number("battery_float_voltage", 54.5)
    await number("battery_temperature_compensation", -30)
    await hass.services.async_call(
        "select", "select_option", {"entity_id": "select.deye_inverter_battery_control_mode", "option": "lithium"},
        blocking=True,
    )
    assert link.writes == [(185, 2535), (188, 4750), (101, 5450), (107, 0x10000 - 30), (98, 1)]
    await hass.async_block_till_done()
    assert hass.states.get("number.deye_inverter_grid_protection_voltage_high").state == "253.5"


async def test_the_dangerous_settings_stay_behind_allow_writes(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    with pytest.raises(HomeAssistantError, match="writes are off"):
        await hass.services.async_call(
            "number", "set_value", {"entity_id": "number.deye_inverter_grid_protection_voltage_high", "value": 253},
            blocking=True,
        )
    assert entry.runtime_data.link.writes == []


async def _options(hass: HomeAssistant, entry: MockConfigEntry, **changes):
    """Submit the options page with these values changed, the rest as shown."""
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    values = {opt.section: {} for opt in o.OPTIONS}
    for opt in o.OPTIONS:
        values[opt.section][opt.key] = changes.get(opt.key, entry.options.get(opt.key, opt.default))
    return await hass.config_entries.options.async_configure(result["flow_id"], values)


async def test_the_options_change_what_the_code_does(hass: HomeAssistant) -> None:
    entry = await _add(hass, writes=True)
    result = await _options(
        hass, entry, battery_current_max=100, zero_export_max=50, power_step=10, low_soc_min=20, grid_min_voltage=300,
        modbus_slave=2, max_read=8, reply_timeout=5, connect_attempts=2, write_characteristic=WRITE_UUID,
        settings_interval=600,
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["battery_current_max"] == 100  # flat in the entry
    assert entry.state is ConfigEntryState.LOADED

    number = hass.states.get("number.deye_inverter_battery_max_charge_current").attributes
    assert number["max"] == 100
    assert hass.states.get("number.deye_inverter_zero_export_power").attributes["max"] == 50
    assert hass.states.get("number.deye_inverter_max_sell_power").attributes["step"] == 10
    assert hass.states.get("number.deye_inverter_battery_low_soc").attributes["min"] == 20
    assert entry.runtime_data.link.settings == {
        "slave": 2, "max_read": 8, "write_char": WRITE_UUID, "notify_char": p.NOTIFY_CHAR,
        "reply_timeout": 5, "connect_attempts": 2,
    }
    entry.runtime_data.link.regs[552] = 0  # relay open, 230 V is now under the option's 300 V
    entry.runtime_data._fast_at = float("-inf")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.deye_grid").state == "off"


async def test_the_options_page_refuses_bad_values(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    assert (await _options(hass, entry, write_characteristic="nope"))["errors"] == {"base": "bad_uuid"}
    assert (await _options(hass, entry, stale_after=10, scan_interval=15))["errors"] == {"base": "stale_too_short"}
    assert entry.options.get("write_characteristic") is None


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


async def test_link_splits_reads_to_the_option() -> None:
    link = Link(slave=2, max_read=3, write_char=p.WRITE_CHAR, notify_char=p.NOTIFY_CHAR, reply_timeout=1, connect_attempts=1)
    sent = []

    async def command(payload: bytes) -> str:
        sent.append(payload)
        frame = bytes.fromhex(payload.decode().split(",")[1])
        assert frame[0] == 2  # the slave of the option
        count = frame[5]
        body = bytes((2, 3, 2 * count)) + bytes(2 * count)
        return "+ok=" + (body + p.crc16(body)).hex()

    link._command = command
    assert await link.read(100, 7) == [0] * 7
    assert len(sent) == 3


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


async def test_flow_by_hand_while_a_discovery_of_the_logger_waits(hass: HomeAssistant) -> None:
    info = MagicMock(address=ADDRESS, service_uuids=["00000922-0000-1000-8000-00805f9b34fb"])
    info.name = "logger"
    discovery = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "bluetooth"}, data=info)
    assert discovery["type"] is FlowResultType.FORM
    with patch("custom_components.deye_bluetooth.config_flow.async_discovered_service_info", return_value=[info]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDRESS})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert hass.config_entries.flow.async_progress() == []  # the discovery ended with it


async def test_flow_without_a_logger_aborts(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "homeassistant", {})
    with patch("custom_components.deye_bluetooth.config_flow.async_discovered_service_info", return_value=[]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "no_devices_found"

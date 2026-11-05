"""The integration inside a real Home Assistant, with the Bluetooth link faked."""
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_bluetooth import DOMAIN
from custom_components.deye_bluetooth import options as o
from custom_components.deye_bluetooth import protocol as p
from custom_components.deye_bluetooth import registers as r
from custom_components.deye_bluetooth.diagnostics import async_get_config_entry_diagnostics
from custom_components.deye_bluetooth.transport import Link, LinkError

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
    assert set(changes) <= {opt.key for opt in o.OPTIONS}, f"unknown option(s): {set(changes) - {opt.key for opt in o.OPTIONS}}"
    values = {opt.section: {} for opt in o.OPTIONS}
    for opt in o.OPTIONS:
        values[opt.section][opt.key] = changes.get(opt.key, entry.options.get(opt.key, opt.default))
    return await hass.config_entries.options.async_configure(result["flow_id"], values)


async def test_the_options_change_what_the_code_does(hass: HomeAssistant) -> None:
    entry = await _add(hass, writes=True)
    result = await _options(
        hass, entry, modbus_slave=2, max_read=8, reply_timeout=5, connect_attempts=2,
        write_characteristic=WRITE_UUID, settings_interval=600,
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert entry.options["settings_interval"] == 600  # flat in the entry
    assert entry.state is ConfigEntryState.LOADED

    # the inverter's own settings keep the register's range, not an option of the link
    assert hass.states.get("number.deye_inverter_battery_low_soc").attributes["min"] == 0
    assert hass.states.get("number.deye_inverter_battery_max_charge_current").attributes["max"] == 2000
    assert hass.states.get("number.deye_inverter_zero_export_power").attributes["max"] == 10000
    assert hass.states.get("number.deye_inverter_max_sell_power").attributes["step"] == 100
    assert entry.runtime_data.link.settings == {
        "slave": 2, "max_read": 8, "write_char": WRITE_UUID, "notify_char": p.NOTIFY_CHAR,
        "reply_timeout": 5, "connect_attempts": 2,
    }
    entry.runtime_data.link.regs[552] = 0  # relay open, but 230 V is still grid present
    entry.runtime_data._fast_at = float("-inf")
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.deye_grid").state == "on"


async def test_a_low_soc_below_ten_can_be_set(hass: HomeAssistant) -> None:
    """The inverter can report 5 %: no option may clamp the register below its range."""
    entry = await _add(hass, writes=True)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.deye_inverter_battery_low_soc", "value": 5}, blocking=True
    )
    assert entry.runtime_data.link.writes == [(117, 5)]


async def test_every_numeric_option_is_an_input_box(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    boxes = [
        validator
        for group in result["data_schema"].schema.values()
        if isinstance(group, section)
        for validator in group.schema.schema.values()
        if isinstance(validator, selector.NumberSelector)
    ]
    assert len(boxes) == len([option for option in o.OPTIONS if option.low is not None])
    assert all(box.config["mode"] == selector.NumberSelectorMode.BOX for box in boxes)


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


class _FakeGatt:
    """A logger whose console is one write and one notify characteristic."""

    def __init__(self, chars: dict[str, list[str]], console: tuple[str, str]) -> None:
        char_objects = [SimpleNamespace(uuid=uuid, properties=props) for uuid, props in chars.items()]
        self.services = [SimpleNamespace(characteristics=char_objects)]
        self.console = console
        self.is_connected = True
        self.written: list[str] = []
        self.notifying: dict[str, object] = {}

    async def start_notify(self, char: str, callback) -> None:
        self.notifying[char] = callback

    async def stop_notify(self, char: str) -> None:
        del self.notifying[char]

    async def write_gatt_char(self, char: str, payload: bytes, response: bool) -> None:
        self.written.append(char)
        if char == self.console[0] and self.console[1] in self.notifying:
            self.notifying[self.console[1]](None, bytearray(b"+ok=21521,21521\n"))

    async def disconnect(self) -> None:
        self.is_connected = False


async def _connect(gatt: _FakeGatt) -> Link:
    link = Link(slave=1, max_read=16, write_char=p.WRITE_CHAR, notify_char=p.NOTIFY_CHAR, reply_timeout=0.01, connect_attempts=1)
    with patch("custom_components.deye_bluetooth.transport.establish_connection", new=AsyncMock(return_value=gatt)):
        await link.connect(MagicMock())
    return link


async def test_the_configured_characteristics_are_used_without_a_search() -> None:
    other = "0000aaaa-0000-1000-8000-00805f9b34fb"
    chars = {other: ["write", "notify"], p.WRITE_CHAR: ["write"], p.NOTIFY_CHAR: ["notify"]}
    gatt = _FakeGatt(chars, (p.WRITE_CHAR, p.NOTIFY_CHAR))
    link = await _connect(gatt)
    assert link.connected
    assert gatt.written == [p.WRITE_CHAR]


async def test_other_characteristics_are_found_by_the_handshake() -> None:
    write, notify, decoy = (f"0000{n}-0000-1000-8000-00805f9b34fb" for n in ("aaa1", "aaa2", "aaa3"))
    gatt = _FakeGatt({decoy: ["write", "notify"], write: ["write"], notify: ["notify"]}, (write, notify))
    link = await _connect(gatt)
    assert link.connected
    assert gatt.written[-1] == write
    assert list(gatt.notifying) == [notify]  # the decoy does not stay subscribed
    assert (link._write_char, link._notify_char) == (write, notify)


async def test_a_logger_without_a_console_fails_and_lets_go_of_the_link() -> None:
    uuid = "0000aaa1-0000-1000-8000-00805f9b34fb"
    gatt = _FakeGatt({uuid: ["write", "notify"]}, ("none", "none"))
    with pytest.raises(LinkError, match="no console"):
        await _connect(gatt)
    assert not gatt.is_connected


async def test_a_handshake_failure_on_the_configured_pair_does_not_start_a_search() -> None:
    other = "0000aaa1-0000-1000-8000-00805f9b34fb"
    gatt = _FakeGatt({other: ["write", "notify"], p.WRITE_CHAR: ["write"], p.NOTIFY_CHAR: ["notify"]}, (other, other))
    with pytest.raises(LinkError):
        await _connect(gatt)
    assert gatt.written == [p.WRITE_CHAR]


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


# --- behavioural coverage added by the review loops -------------------------
SERVICE_UUID = "00000922-0000-1000-8000-00805f9b34fb"
REMOVED_OPTIONS = {"grid_min_voltage", "battery_current_max", "zero_export_max", "power_step", "low_soc_min"}


def test_service_number_fields_are_input_boxes():
    path = Path(__file__).resolve().parents[1] / "custom_components/deye_bluetooth/services.yaml"
    spec = yaml.safe_load(path.read_text())
    for service, body in spec.items():
        for field, cfg in body["fields"].items():
            number = cfg.get("selector", {}).get("number")
            if number:
                assert number["mode"] == "box", (service, field)
    count = spec["read_registers"]["fields"]["count"]["selector"]["number"]
    assert (count["min"], count["max"], count["mode"]) == (1, 125, "box")


def test_defaults_match_the_thirteen_options():
    assert len(o.OPTIONS) == 13
    assert o.SECTIONS == {o.POLLING: False, o.CONTROL: False, o.LINK: True}
    assert o.DEFAULTS == {
        o.LIVE_S: 1, o.SCAN_S: 15, o.SETTINGS_S: 300, o.STALE_S: 120, o.SETTINGS_STALE_S: 900,
        o.WRITES: False, o.WRITE_ATTEMPTS: 3, o.SLAVE: p.SLAVE, o.MAX_READ: p.MAX_READ,
        o.REPLY_TIMEOUT: 10, o.CONNECT_ATTEMPTS: 3, o.WRITE_CHAR: p.WRITE_CHAR, o.NOTIFY_CHAR: p.NOTIFY_CHAR,
    }
    assert REMOVED_OPTIONS.isdisjoint(o.DEFAULTS)
    assert {opt.key for opt in o.OPTIONS if opt.low is None} == {o.WRITES, o.WRITE_CHAR, o.NOTIFY_CHAR}
    for opt in o.OPTIONS:
        assert opt.section in o.SECTIONS
        if opt.low is not None:
            assert opt.low <= opt.default <= opt.high
            assert opt.low <= opt.high


async def test_removed_limits_stay_gone_for_a_legacy_entry(hass: HomeAssistant) -> None:
    stale = dict.fromkeys(REMOVED_OPTIONS, 999) | {o.WRITES: True}
    entry = MockConfigEntry(
        domain=DOMAIN, unique_id=ADDRESS, data={"address": ADDRESS, "entity_prefix": "deye"},
        options=stale, version=1,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert REMOVED_OPTIONS.isdisjoint(entry.options)  # async_migrate_entry strips them
    assert entry.version == 2
    assert entry.runtime_data.option(o.SCAN_S) == 15
    assert hass.states.get("number.deye_inverter_battery_low_soc").attributes["min"] == 0
    assert hass.states.get("number.deye_inverter_battery_max_charge_current").attributes["max"] == 2000


async def test_grid_present_threshold_is_fixed_at_100_volts(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    link, coordinator = entry.runtime_data.link, entry.runtime_data

    async def grid(raw_volts, relay):
        link.regs[552] = relay
        for reg in (598, 599, 600):
            link.regs[reg] = raw_volts
        coordinator._fast_at = float("-inf")
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        return hass.states.get("binary_sensor.deye_grid").state

    assert await grid(1000, 0) == "on"                # exactly 100.0 V
    assert await grid(999, 0) == "off"                # 99.9 V is below the fixed threshold
    assert await grid(0, r.GRID_RELAY_BIT) == "on"    # closed relay wins regardless of volts


async def test_options_validation_all_error_branches(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    assert (await _options(hass, entry, notify_characteristic="nope"))["errors"] == {"base": "bad_uuid"}
    assert (await _options(hass, entry, settings_stale_after=60, settings_interval=300))["errors"] == {
        "base": "stale_too_short"
    }
    assert (await _options(hass, entry, write_characteristic="nope", stale_after=10, scan_interval=15))["errors"] == {
        "base": "bad_uuid"
    }
    assert (await _options(hass, entry, stale_after=15, scan_interval=15))["type"] is FlowResultType.CREATE_ENTRY
    assert (await _options(hass, entry, write_characteristic="x" + WRITE_UUID))["errors"] == {"base": "bad_uuid"}


async def test_options_error_redisplays_the_bad_value(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    result = await _options(hass, entry, write_characteristic="nope")
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "bad_uuid"}
    shown = [
        marker.default()
        for group in result["data_schema"].schema.values()
        if isinstance(group, section)
        for marker in group.schema.schema
        if marker.schema == o.WRITE_CHAR
    ]
    assert shown == ["nope"]
    assert entry.options.get(o.WRITE_CHAR) is None


async def test_options_form_sections_defaults_and_selectors(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    groups = {m.schema: g for m, g in result["data_schema"].schema.items() if isinstance(g, section)}
    assert set(groups) == set(o.SECTIONS)
    for name, group in groups.items():
        assert group.options["collapsed"] == o.SECTIONS[name]
        fields = {m.schema: (m.default(), v) for m, v in group.schema.schema.items()}
        assert set(fields) == {opt.key for opt in o.OPTIONS if opt.section == name}
        for opt in (opt for opt in o.OPTIONS if opt.section == name):
            assert fields[opt.key][0] == o.DEFAULTS[opt.key]
        validators = {m.schema: v for m, v in group.schema.schema.items()}
        if name == o.CONTROL:
            assert validators[o.WRITES] is bool
        if name == o.LINK:
            assert validators[o.WRITE_CHAR] is str
            assert validators[o.NOTIFY_CHAR] is str

    boxes = {
        m.schema: v
        for group in groups.values()
        for m, v in group.schema.schema.items()
        if isinstance(v, selector.NumberSelector)
    }
    assert set(boxes) == {opt.key for opt in o.OPTIONS if opt.low is not None}
    for opt in o.OPTIONS:
        if opt.low is None:
            continue
        box = boxes[opt.key]
        assert box.config["min"] == opt.low
        assert box.config["max"] == opt.high
        assert box.config["step"] == 1
        assert box.config["mode"] == selector.NumberSelectorMode.BOX
        assert isinstance(box(opt.default), int)
        with pytest.raises(Exception, match="whole number"):
            box(opt.default + 0.5)


async def test_options_saved_values_become_form_defaults(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    assert (await _options(hass, entry, scan_interval=42))["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(entry.entry_id)
    defaults = {
        m.schema: m.default()
        for group in result["data_schema"].schema.values()
        if isinstance(group, section)
        for m in group.schema.schema
    }
    assert defaults[o.SCAN_S] == 42


async def test_a_configured_logger_cannot_be_discovered_twice(hass: HomeAssistant) -> None:
    await _add(hass)
    info = MagicMock(address=ADDRESS, service_uuids=[SERVICE_UUID])
    info.name = "logger"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "bluetooth"}, data=info)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_bluetooth_discovery_normalises_address_and_title(hass: HomeAssistant) -> None:
    info = MagicMock(address="aa:bb:cc:dd:ee:01", service_uuids=[SERVICE_UUID])
    info.name = "logger"
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "bluetooth"}, data=info)
    assert result["type"] is FlowResultType.FORM
    assert hass.config_entries.flow.async_progress()[0]["context"]["title_placeholders"] == {"name": "logger"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDRESS})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"]["address"] == ADDRESS
    assert hass.config_entries.async_entries(DOMAIN)[0].unique_id == ADDRESS


async def test_user_form_offers_only_unconfigured_deye_loggers(hass: HomeAssistant) -> None:
    await _add(hass)
    foreign = MagicMock(address="11:11:11:11:11:11", service_uuids=["deadbeef"])
    configured = MagicMock(address=ADDRESS, service_uuids=[SERVICE_UUID])
    valid = MagicMock(address=ADDRESS_2, service_uuids=[SERVICE_UUID])
    valid.name = None
    discovered = [foreign, configured, valid]
    with patch("custom_components.deye_bluetooth.config_flow.async_discovered_service_info",
              return_value=discovered):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDRESS_2})
    assert result["title"] == f"Deye logger {ADDRESS_2}"
    with patch("custom_components.deye_bluetooth.config_flow.async_discovered_service_info",
              return_value=[foreign, configured]):
        empty = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
    assert empty["type"] is FlowResultType.ABORT
    assert empty["reason"] == "no_devices_found"


async def test_write_register_writes_a_list_to_consecutive_registers(hass: HomeAssistant) -> None:
    entry = await _add(hass, writes=True)
    link = entry.runtime_data.link
    await hass.services.async_call(DOMAIN, "write_register", {"address": 166, "value": [20, 30, 40]}, blocking=True)
    assert link.writes == [(166, 20), (167, 30), (168, 40)]
    link.writes.clear()
    link.regs[166] = 20  # already equal: the unchanged registers must be skipped
    link.regs[167] = 30
    link.regs[168] = 40
    await hass.services.async_call(DOMAIN, "write_register", {"address": 166, "value": [20, 30, 40]}, blocking=True)
    assert link.writes == []


async def test_config_entry_id_rejects_an_unknown_logger(hass: HomeAssistant) -> None:
    await _add(hass)
    with pytest.raises(ServiceValidationError, match="no loaded logger bogus"):
        await hass.services.async_call(
            DOMAIN, "read_registers", {"address": 607, "config_entry_id": "bogus"},
            blocking=True, return_response=True,
        )


async def test_unload_one_of_two_closes_only_its_link(hass: HomeAssistant) -> None:
    first = await _add(hass)
    second = await _add(hass, ADDRESS_2, "deye_2")
    first_link = first.runtime_data.link
    assert await hass.config_entries.async_unload(first.entry_id)
    await hass.async_block_till_done()
    assert first.state is ConfigEntryState.NOT_LOADED
    assert first_link.connected is False
    assert second.state is ConfigEntryState.LOADED
    assert hass.states.get("sensor.deye_grid_power").state == "unavailable"
    assert hass.states.get("sensor.deye_2_grid_power").state != "unavailable"

"""The options page, the config flow and the options migration, inside a real Home Assistant."""
from unittest.mock import MagicMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, section
from homeassistant.helpers import selector
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_bluetooth import DOMAIN, FAST_GROUP
from custom_components.deye_bluetooth import options as o
from custom_components.deye_bluetooth import protocol as p
from custom_components.deye_bluetooth.config_flow import SERVICE_UUID
from tests.support import ADDRESS, ADDRESS_2, WRITE_UUID, _add, _due, _options


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
    _due(entry.runtime_data, FAST_GROUP)
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.deye_grid").state == "on"


async def test_the_options_page_refuses_bad_values(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    assert (await _options(hass, entry, write_characteristic="nope"))["errors"] == {"base": "bad_uuid"}
    assert (await _options(hass, entry, stale_after=10, scan_interval=15))["errors"] == {"base": "stale_too_short"}
    assert entry.options.get("write_characteristic") is None


async def test_a_stale_limit_shorter_than_the_live_interval_is_refused(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    result = await _options(hass, entry, stale_after=10, scan_interval=10, live_interval=15)
    assert result["errors"] == {"base": "stale_too_short"}  # the live values would be unavailable between two reads


async def test_flow_gives_the_second_logger_its_own_prefix(hass: HomeAssistant) -> None:
    await _add(hass)
    info = MagicMock(address=ADDRESS_2, service_uuids=[SERVICE_UUID])
    info.name = "logger2"
    with patch("custom_components.deye_bluetooth.config_flow.async_discovered_service_info", return_value=[info]):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": "user"})
        assert result["type"] is FlowResultType.FORM
        result = await hass.config_entries.flow.async_configure(result["flow_id"], {"address": ADDRESS_2})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["data"] == {"address": ADDRESS_2, "entity_prefix": "deye_2"}


async def test_flow_by_hand_while_a_discovery_of_the_logger_waits(hass: HomeAssistant) -> None:
    info = MagicMock(address=ADDRESS, service_uuids=[SERVICE_UUID])
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
REMOVED_OPTIONS = {"grid_min_voltage", "battery_current_max", "zero_export_max", "power_step", "low_soc_min"}


def test_defaults_match_the_fourteen_options():
    assert len(o.OPTIONS) == 14
    assert o.SECTIONS == {o.POLLING: False, o.CONTROL: False, o.LINK: True}
    assert o.DEFAULTS == {
        o.LIVE_S: 1, o.SCAN_S: 15, o.SETTINGS_S: 300, o.STALE_S: 120, o.SETTINGS_STALE_S: 900, o.ALERT_S: 300,
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

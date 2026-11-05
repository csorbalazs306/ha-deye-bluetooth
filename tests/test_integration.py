"""The integration inside a real Home Assistant: setup, entities, services and the write rules."""
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import Context, HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError, Unauthorized
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_bluetooth import DOMAIN, FAST_GROUP, SLOW_GROUP
from custom_components.deye_bluetooth import options as o
from custom_components.deye_bluetooth import registers as r
from custom_components.deye_bluetooth.diagnostics import async_get_config_entry_diagnostics
from tests.support import ADDRESS, ADDRESS_2, NOTIFY_UUID, WRITE_UUID, FakeLink, _add, _age, _due, _refresh


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

    with pytest.raises(ServiceValidationError, match="Found multiple config entries"):
        await hass.services.async_call(DOMAIN, "write_register", {"address": 145, "value": 1}, blocking=True)
    await hass.services.async_call(
        DOMAIN, "write_register", {"address": 145, "value": 1, "config_entry_id": second.entry_id}, blocking=True
    )
    assert second.runtime_data.link.writes == [(145, 1)]
    assert first.runtime_data.link.writes == []

    # the id is needed as long as both entries exist, loaded or not (core counts every entry of the integration)
    await hass.config_entries.async_unload(first.entry_id)
    with pytest.raises(ServiceValidationError, match="Found multiple config entries"):
        await hass.services.async_call(DOMAIN, "write_register", {"address": 145, "value": 0}, blocking=True)
    assert second.runtime_data.link.writes == [(145, 1)]
    await hass.services.async_call(
        DOMAIN, "write_register", {"address": 145, "value": 0, "config_entry_id": second.entry_id}, blocking=True
    )
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
    _due(coordinator, SLOW_GROUP)
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


async def test_a_low_soc_below_ten_can_be_set(hass: HomeAssistant) -> None:
    """The inverter can report 5 %: no option may clamp the register below its range."""
    entry = await _add(hass, writes=True)
    await hass.services.async_call(
        "number", "set_value", {"entity_id": "number.deye_inverter_battery_low_soc", "value": 5}, blocking=True
    )
    assert entry.runtime_data.link.writes == [(117, 5)]


async def test_read_registers_action(hass: HomeAssistant) -> None:
    await _add(hass)
    result = await hass.services.async_call(
        DOMAIN, "read_registers", {"address": 607, "count": 1}, blocking=True, return_response=True
    )
    assert result == {"address": 607, "values": [32]}


async def test_unknown_model_warns(hass: HomeAssistant, caplog) -> None:
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


async def test_grid_present_threshold_is_fixed_at_100_volts(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    link, coordinator = entry.runtime_data.link, entry.runtime_data

    async def grid(raw_volts, relay):
        link.regs[552] = relay
        for reg in (598, 599, 600):
            link.regs[reg] = raw_volts
        _due(coordinator, FAST_GROUP)
        await coordinator.async_refresh()
        await hass.async_block_till_done()
        return hass.states.get("binary_sensor.deye_grid").state

    assert await grid(1000, 0) == "on"                # exactly 100.0 V
    assert await grid(999, 0) == "off"                # 99.9 V is below the fixed threshold
    assert await grid(0, r.GRID_RELAY_BIT) == "on"    # closed relay wins regardless of volts


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
    with pytest.raises(ServiceValidationError, match="config entry with ID bogus was not found"):
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


# --- the behaviour of the batch: writes, backoff, refusals, scan, settings and availability -------------------------
async def test_a_write_rereads_only_the_blocks_it_touched(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass, writes=True)
    link = entry.runtime_data.link
    link.reads.clear()
    await hass.services.async_call(DOMAIN, "write_register", {"address": 145, "value": 1}, blocking=True)
    await hass.async_block_till_done()
    assert link.writes == [(145, 1)]
    # the write's own read and read-back, then the next poll: the live blocks and the one block of register 145
    assert link.reads == [(145, 1), (145, 1), (604, 9), (650, 6), (130, 16)]


async def test_a_write_to_no_poll_block_needs_no_reread(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass, writes=True)
    link = entry.runtime_data.link
    link.reads.clear()
    await hass.services.async_call(DOMAIN, "write_register", {"address": 601, "value": 7}, blocking=True)
    await hass.async_block_till_done()
    assert link.writes == [(601, 7)]
    assert link.reads == [(601, 1), (601, 1), (604, 9), (650, 6)]  # 601 is in no poll block


async def test_a_write_that_fails_midway_leaves_its_blocks_for_the_next_poll(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass, writes=True, write_attempts=1)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    link.fail_write_at = 162
    with pytest.raises(HomeAssistantError, match="write to 161 failed"):
        await hass.services.async_call(DOMAIN, "write_register", {"address": 161, "value": [5, 6]}, blocking=True)
    assert link.writes == [(161, 5)]  # 161 landed, 162 was lost on the link
    assert not link.connected  # a link error closes the link

    link.fail_write_at = None
    link.reads.clear()
    await _refresh(hass, coordinator)
    # the blocks of both registers the write reached, and nothing else of the settings
    assert link.reads == [(604, 9), (650, 6), (146, 16), (162, 16)]
    link.reads.clear()
    await _refresh(hass, coordinator)
    assert link.reads == [(604, 9), (650, 6)]  # read once, then cleared


async def test_a_pending_fast_block_is_shown_on_the_next_poll(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass, writes=True)
    await hass.services.async_call(DOMAIN, "write_register", {"address": 514, "value": 5}, blocking=True)
    await hass.async_block_till_done()
    # a fast-poll register: its non-settings entity is written on the poll that re-read it
    assert hass.states.get("sensor.deye_inverter_daily_charge").state == "0.5"
    assert entry.runtime_data.link.writes == [(514, 5)]


async def test_the_grid_charge_switch_asks_for_one_poll(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass, writes=True)
    coordinator = entry.runtime_data
    asked: list[int] = []
    request = coordinator.async_request_refresh

    async def counting() -> None:
        asked.append(1)
        await request()

    with patch.object(coordinator, "async_request_refresh", counting):
        await hass.services.async_call("switch", "turn_on", {"entity_id": "switch.deye_grid_charge"}, blocking=True)
    await hass.async_block_till_done()
    assert len(asked) == 1
    assert coordinator.link.writes[-1] == (130, 1)  # the enable was written after the flags


async def test_a_refused_read_keeps_the_link(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    link = entry.runtime_data.link
    link.rejected.add(999)
    connects = link.connects
    with pytest.raises(HomeAssistantError, match="read of 999 failed"):
        await hass.services.async_call(
            DOMAIN, "read_registers", {"address": 999}, blocking=True, return_response=True
        )
    assert link.connected and link.connects == connects


async def test_a_refused_write_is_not_retried_and_keeps_the_link(hass: HomeAssistant) -> None:
    entry = await _add(hass, writes=True)
    link = entry.runtime_data.link
    link.refused_writes.add(145)
    with pytest.raises(HomeAssistantError, match="write to 145 failed"):
        await hass.services.async_call(DOMAIN, "write_register", {"address": 145, "value": 1}, blocking=True)
    assert link.write_calls == 1  # three attempts are for lost replies, not for refusals
    assert link.connected
    assert link.writes == []


async def test_a_found_console_is_kept_in_the_entry_without_a_reload(hass: HomeAssistant) -> None:
    with patch.object(FakeLink, "scan_result", (WRITE_UUID, NOTIFY_UUID)):
        entry = await _add(hass)
    coordinator = entry.runtime_data
    await hass.async_block_till_done()
    assert (entry.options[o.WRITE_CHAR], entry.options[o.NOTIFY_CHAR]) == (WRITE_UUID, NOTIFY_UUID)
    assert (coordinator._options[o.WRITE_CHAR], coordinator._options[o.NOTIFY_CHAR]) == (WRITE_UUID, NOTIFY_UUID)
    assert entry.runtime_data is coordinator  # no reload: the same coordinator and link
    assert entry.state is ConfigEntryState.LOADED
    assert not entry.update_listeners

    await hass.config_entries.async_reload(entry.entry_id)  # the next start uses the pair without a scan
    assert entry.runtime_data.link.settings["write_char"] == WRITE_UUID
    assert entry.runtime_data.link.found_console is None


async def test_a_detail_sensor_that_repeats_a_control_or_summary_is_disabled_by_default(hass: HomeAssistant) -> None:
    entry = await _add(hass)
    registry = er.async_get(hass)

    def disabled(entity_id: str) -> bool:
        return registry.async_get(entity_id).disabled

    assert disabled("sensor.deye_inverter_grid_power_l1")  # the source of deye_grid_phase_a_power
    assert disabled("sensor.deye_inverter_battery_capacity")  # a number control and a summary source
    assert disabled("sensor.deye_inverter_work_mode")  # the select of the same register
    assert disabled("sensor.deye_inverter_program_1_time")  # a time control
    assert not disabled("sensor.deye_inverter_pv3_power")  # neither a control nor a summary source
    assert hass.states.get("sensor.deye_inverter_pv3_power") is not None
    assert hass.states.get("sensor.deye_inverter_grid_power_l1") is None
    duplicates = {e.entity_id for e in registry.entities.values() if e.config_entry_id == entry.entry_id and e.disabled}
    assert len(duplicates) == 113  # 94 controls, 19 summary sources that are no control
    assert all(entity_id.startswith("sensor.deye_inverter_") for entity_id in duplicates)


async def test_the_work_mode_name_is_unavailable_while_its_register_is_missing(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass)
    assert hass.states.get("sensor.deye_inverter_work_mode_name").state == "selling first"
    _age(entry.runtime_data, 130, 10_000)  # the block that holds register 142 is no longer fresh
    await _refresh(hass, entry.runtime_data)
    assert hass.states.get("sensor.deye_inverter_work_mode_name").state == "unavailable"  # not "unknown"

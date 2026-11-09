"""The polling behaviour inside a real Home Assistant: reconnect backoff, settings, availability and the alert."""
from unittest.mock import patch

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.deye_bluetooth import DOMAIN
from tests.support import ADDRESS, _add, _age, _refresh, _written

OUT_OF_RANGE = patch("custom_components.deye_bluetooth.bluetooth.async_ble_device_from_address", return_value=None)


async def test_a_dropped_link_is_not_reconnected_during_its_backoff(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    link.fail_reads = True
    await _refresh(hass, coordinator)  # t=1000: the read fails and the link is closed, a 2 s wait starts
    assert (coordinator.failures, link.connected) == (1, False)
    link.fail_reads = False
    connects = link.connects

    clock[0] = 1001.0  # inside the wait: no connection attempt, and the failure is not counted again
    await _refresh(hass, coordinator)
    assert link.connects == connects
    assert (coordinator.failures, coordinator.last_error) == (1, "no reply (test)")

    clock[0] = 1002.0  # the wait is over
    await _refresh(hass, coordinator)
    assert link.connects == connects + 1
    assert (coordinator.failures, coordinator.last_error, link.connected) == (0, "", True)


async def test_failed_connections_wait_longer_each_time_up_to_a_minute(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    link.fail_reads = True
    await _refresh(hass, coordinator)  # t=1000: the link drops, the first wait is 2 s
    link.fail_reads = False
    link.fail_connect = True
    connects = link.connects
    now = 1002.0
    for failures, wait in ((2, 4), (3, 8), (4, 16), (5, 32), (6, 60), (7, 60)):
        clock[0] = now  # the attempt is due: it fails, and the next wait is 2 ** failures seconds
        await _refresh(hass, coordinator)
        assert (coordinator.failures, link.connects) == (failures, connects + failures - 1)
        clock[0] = now + wait - 1  # one second short of the wait: skipped, nothing counted
        await _refresh(hass, coordinator)
        assert (coordinator.failures, link.connects) == (failures, connects + failures - 1)
        now += wait


async def test_a_logger_out_of_range_is_looked_for_at_every_poll(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    await link.close()  # the link has dropped
    connects = link.connects
    with patch("custom_components.deye_bluetooth.bluetooth.async_ble_device_from_address", return_value=None):
        await _refresh(hass, coordinator)
        assert coordinator.failures == 1
        assert "not in range" in coordinator.last_error
        await _refresh(hass, coordinator)  # no wait after a logger that is out of range
        assert coordinator.failures == 2
    assert link.connects == connects  # nothing was tried while it was out of range

    await _refresh(hass, coordinator)  # back in range at the same moment: connected at once
    assert (link.connects, coordinator.failures) == (connects + 1, 0)


async def test_the_first_failure_after_an_out_of_range_spell_waits_two_seconds(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    await link.close()  # the link has dropped
    with OUT_OF_RANGE:
        for _ in range(8):
            await _refresh(hass, coordinator)
    assert coordinator.failures == 8

    link.fail_connect = True  # back in range, but the connection fails: the first real failure
    await _refresh(hass, coordinator)
    assert coordinator.failures == 9
    clock[0] += 2  # the wait is 2 s, not the 60 s that 9 failures would give
    link.fail_connect = False
    await _refresh(hass, coordinator)
    assert (link.connected, coordinator.failures) == (True, 0)


async def test_the_read_action_connects_during_a_backoff(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    link.fail_reads = True
    await _refresh(hass, coordinator)  # the backoff is on
    link.fail_reads = False
    connects = link.connects
    result = await hass.services.async_call(
        DOMAIN, "read_registers", {"address": 607, "count": 1}, blocking=True, return_response=True
    )
    assert result == {"address": 607, "values": [32]}
    assert link.connects == connects + 1


async def test_a_settings_entity_is_written_on_a_settings_poll_only(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass)
    coordinator = entry.runtime_data
    with _written() as written:
        clock[0] += 16  # a fast poll: the settings are not read
        await _refresh(hass, coordinator)
    assert "sensor.deye_grid_power" in written  # live
    assert "sensor.deye_inverter_battery_temperature" in written  # read on this poll, and no setting
    assert "number.deye_inverter_battery_low_soc" not in written

    with _written() as written:
        clock[0] += 300  # a settings poll
        await _refresh(hass, coordinator)
    assert "number.deye_inverter_battery_low_soc" in written


async def test_a_setting_goes_unavailable_when_stale_without_a_settings_poll(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass)
    coordinator = entry.runtime_data
    assert hass.states.get("number.deye_inverter_battery_low_soc").state == "10"
    _age(coordinator, 114, 1000)  # its block is older than the settings_stale_after option (900 s)
    with _written() as written:
        clock[0] += 16  # a fast poll: the settings block is not due, so it is not read again
        await _refresh(hass, coordinator)
    assert hass.states.get("number.deye_inverter_battery_low_soc").state == "unavailable"
    assert "number.deye_inverter_battery_low_soc" in written  # its availability changed: written at once
    assert "number.deye_inverter_max_sell_power" not in written  # fresh, and not a settings poll

    with _written() as written:
        clock[0] += 300  # the settings poll reads the block again: the value is back
        await _refresh(hass, coordinator)
    assert hass.states.get("number.deye_inverter_battery_low_soc").state == "10"
    assert "number.deye_inverter_battery_low_soc" in written


async def test_a_value_goes_unavailable_within_one_poll_when_its_block_is_stale(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass)
    coordinator = entry.runtime_data
    temperature = "sensor.deye_inverter_battery_temperature"  # the battery block, 586: fast, not a duplicate
    assert hass.states.get(temperature).state != "unavailable"
    _age(coordinator, 586, 1000)  # stale after 120 s
    clock[0] += 1  # no fast poll is due, so the block is not read again
    await _refresh(hass, coordinator)
    assert hass.states.get(temperature).state == "unavailable"

    clock[0] += 15  # the fast poll reads the block again: available again
    await _refresh(hass, coordinator)
    assert hass.states.get(temperature).state != "unavailable"


def _issue(hass: HomeAssistant, entry) -> ir.IssueEntry | None:
    return ir.async_get(hass).async_get_issue(DOMAIN, f"link_down_{entry.entry_id}")


async def test_a_logger_silent_for_the_alert_delay_raises_a_repairs_issue_until_it_answers(
    hass: HomeAssistant, clock
) -> None:
    entry = await _add(hass)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    link.fail_reads = True
    await _refresh(hass, coordinator)  # t=1000: the first unanswered poll
    link.fail_reads, link.fail_connect = False, True
    for now in (1002.0, 1150.0, 1299.0):  # failed connections, and polls waiting out their backoff
        clock[0] = now
        await _refresh(hass, coordinator)
        assert _issue(hass, entry) is None

    clock[0] = 1300.0  # 300 s, the default delay, with no answer
    await _refresh(hass, coordinator)
    issue = _issue(hass, entry)
    assert issue is not None
    assert (issue.severity, issue.is_fixable, issue.translation_key) == (ir.IssueSeverity.WARNING, False, "link_down")
    assert issue.translation_placeholders == {"name": entry.title, "minutes": "5"}

    link.fail_connect = False
    clock[0] = 1400.0  # past the backoff: it connects and answers, the issue goes
    await _refresh(hass, coordinator)
    assert _issue(hass, entry) is None

    link.fail_reads = True  # a new silence starts its own count
    clock[0] = 1401.0
    await _refresh(hass, coordinator)
    clock[0] = 1700.0
    await _refresh(hass, coordinator)
    assert _issue(hass, entry) is None
    clock[0] = 1701.0
    await _refresh(hass, coordinator)
    assert _issue(hass, entry) is not None


async def test_a_logger_that_never_answers_raises_the_issue_across_setup_retries(hass: HomeAssistant, clock) -> None:
    entry = MockConfigEntry(domain=DOMAIN, unique_id=ADDRESS, data={"address": ADDRESS, "entity_prefix": "deye"})
    entry.add_to_hass(hass)
    with OUT_OF_RANGE:  # held by another device: it stops advertising
        assert not await hass.config_entries.async_setup(entry.entry_id)  # t=1000: the first try
        assert entry.state is ConfigEntryState.SETUP_RETRY
        clock[0] = 1300.0
        await hass.config_entries.async_reload(entry.entry_id)  # a retry: a new coordinator, the count goes on
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY
    assert _issue(hass, entry).translation_placeholders == {"name": entry.title, "minutes": "5"}

    assert await hass.config_entries.async_remove(entry.entry_id)  # removed while retrying: never unloaded
    assert _issue(hass, entry) is None


async def test_an_alert_delay_of_zero_raises_no_issue(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass, link_alert_after=0)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    link.fail_reads = link.fail_connect = True
    for now in (1000.0, 2000.0, 90000.0):
        clock[0] = now
        await _refresh(hass, coordinator)
    assert coordinator.failures == 3
    assert _issue(hass, entry) is None


async def test_unloading_the_entry_drops_its_issue(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass, link_alert_after=60)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    link.fail_reads = link.fail_connect = True
    await _refresh(hass, coordinator)
    clock[0] = 1060.0
    await _refresh(hass, coordinator)
    assert _issue(hass, entry).translation_placeholders["minutes"] == "1"
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert _issue(hass, entry) is None


async def test_an_alert_delay_that_is_not_whole_minutes_is_shown_to_a_tenth(hass: HomeAssistant, clock) -> None:
    entry = await _add(hass, link_alert_after=100)
    coordinator, link = entry.runtime_data, entry.runtime_data.link
    link.fail_reads = link.fail_connect = True
    await _refresh(hass, coordinator)
    clock[0] = 1100.0
    await _refresh(hass, coordinator)
    assert _issue(hass, entry).translation_placeholders["minutes"] == "1.7"  # 100 s, not 1.66667

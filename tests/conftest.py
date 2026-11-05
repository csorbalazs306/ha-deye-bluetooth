"""Shared pytest setup: the fake Bluetooth link for every test that runs in Home Assistant, and the clock."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.support import FakeLink


@pytest.fixture(autouse=True)
def _harness(request):
    """The fake Bluetooth link for each test that takes the hass fixture.

    The pure tests (test_pure.py) take no hass and get nothing: they load protocol.py and registers.py directly.
    """
    if "hass" not in request.fixturenames:
        yield
        return
    request.getfixturevalue("enable_custom_integrations")
    with (
        patch("custom_components.deye_bluetooth.Link", new=FakeLink),
        patch("custom_components.deye_bluetooth.bluetooth.async_ble_device_from_address", return_value=MagicMock()),
        patch("homeassistant.setup.async_process_deps_reqs", new=AsyncMock()),
        patch("homeassistant.setup._async_process_dependencies", new=AsyncMock(return_value=[])),
    ):
        yield


@pytest.fixture
def clock():
    """The coordinator's monotonic clock.  The test moves it: no poll waits for real time."""
    now = [1000.0]
    with patch("custom_components.deye_bluetooth.monotonic", side_effect=lambda: now[0]):
        yield now

"""Diagnostics download: what a bug report or a new inverter model needs."""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from . import CONF_ADDRESS
from . import registers as r

TO_REDACT = {CONF_ADDRESS}
SERIAL_REGS = range(r.SERIAL_REG, r.SERIAL_REG + r.SERIAL_WORDS)


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    coordinator = entry.runtime_data
    return {
        "data": async_redact_data(dict(entry.data), TO_REDACT),
        "options": dict(entry.options),
        "poll_failures": coordinator.failures,
        "last_error": coordinator.last_error.replace(coordinator.address, "**REDACTED**"),
        "registers": {reg: word for reg, word in sorted(coordinator.registers().items()) if reg not in SERIAL_REGS},
    }

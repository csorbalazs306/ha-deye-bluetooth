"""Everything the options page sets, with its default and its range.

What the inverter reports itself (rated power, device type) is read from it and
has no option.  What is a fact of the protocol (the register map, the
service UUID of manifest.json) stays in the code.
"""
from __future__ import annotations

from dataclasses import dataclass

from . import protocol as p

POLLING, CONTROL, LIMITS, LINK = "polling", "control", "limits", "link"
SECTIONS = {POLLING: False, CONTROL: False, LIMITS: True, LINK: True}  # section: collapsed

LIVE_S = "live_interval"
SCAN_S = "scan_interval"
SETTINGS_S = "settings_interval"
STALE_S = "stale_after"
SETTINGS_STALE_S = "settings_stale_after"
WRITES = "allow_writes"
WRITE_ATTEMPTS = "write_attempts"
GRID_MIN_V = "grid_min_voltage"
CURRENT_MAX = "battery_current_max"
ZERO_EXPORT_MAX = "zero_export_max"
POWER_STEP = "power_step"
LOW_SOC_MIN = "low_soc_min"
SLAVE = "modbus_slave"
MAX_READ = "max_read"
REPLY_TIMEOUT = "reply_timeout"
CONNECT_ATTEMPTS = "connect_attempts"
WRITE_CHAR = "write_characteristic"
NOTIFY_CHAR = "notify_characteristic"


@dataclass(frozen=True)
class Option:
    key: str
    section: str
    default: int | bool | str
    low: int | None = None
    high: int | None = None


OPTIONS = (
    Option(LIVE_S, POLLING, 1, 1, 15),
    Option(SCAN_S, POLLING, 15, 10, 600),
    Option(SETTINGS_S, POLLING, 300, 60, 3600),
    Option(STALE_S, POLLING, 120, 10, 3600),
    Option(SETTINGS_STALE_S, POLLING, 900, 60, 86400),
    Option(WRITES, CONTROL, False),
    Option(WRITE_ATTEMPTS, CONTROL, 3, 1, 10),
    Option(GRID_MIN_V, LIMITS, 100, 0, 500),
    Option(CURRENT_MAX, LIMITS, 350, 1, 2000),
    Option(ZERO_EXPORT_MAX, LIMITS, 500, 1, 10000),
    Option(POWER_STEP, LIMITS, 100, 1, 1000),
    Option(LOW_SOC_MIN, LIMITS, 10, 0, 100),
    Option(SLAVE, LINK, p.SLAVE, 1, 247),
    Option(MAX_READ, LINK, p.MAX_READ, 1, p.MODBUS_MAX_READ),
    Option(REPLY_TIMEOUT, LINK, 10, 1, 60),
    Option(CONNECT_ATTEMPTS, LINK, 3, 1, 10),
    Option(WRITE_CHAR, LINK, p.WRITE_CHAR),
    Option(NOTIFY_CHAR, LINK, p.NOTIFY_CHAR),
)
DEFAULTS = {option.key: option.default for option in OPTIONS}

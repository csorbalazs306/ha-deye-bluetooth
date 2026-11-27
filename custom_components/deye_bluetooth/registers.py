"""The Deye register maps: what is polled and how it decodes.

Two maps, chosen by the device type in register 0 (map_for):
- the 3-phase LV hybrid map (MODBUS RTU V105, the ha-solarman ``deye_p3`` profile), verified on a
  SUN-15K-SG05LP3-EU-SM2.  The module-level names (LIVE, VALUES, NUMBERS, ...) are this map;
- the single-phase hybrid map (SUN-*K-SG0*LP1, the ha-solarman ``deye_hybrid`` profile, MODBUS RTU V118),
  SINGLE_PHASE at the end of the module.  Not verified on a logger yet.

Holding registers, decimal.  32-bit values are low word first.

Three poll groups: LIVE every second (grid powers and currents, load, battery
SoC and power, PV powers), FAST every scan (15 s: grid voltages, the rest of the
battery and PV, inverter, energy counters), SLOW every
settings interval (identity, battery and Time-of-Use settings).  Every block
is at most 16 registers: the logger does not answer longer reads.

Kept free of Home Assistant imports so the tests run without it.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

# every live interval (1 s): the fast-moving powers of a power flow — grid, load,
# battery and PV
LIVE: list[tuple[int, int]] = [
    (588, 3),   # battery SoC, power
    (604, 9),   # grid powers per phase and total, grid frequency, grid currents
    (650, 6),   # load powers per phase and total, load frequency
    (672, 4),   # PV1-4 power
]
FAST: list[tuple[int, int]] = [
    (514, 16),  # daily / total battery, grid, load energy; daily PV
    (534, 8),   # total PV, DC / AC temperature
    (552, 7),   # relay status, warning and fault words
    (586, 2),   # battery temperature, voltage
    (591, 4),   # battery corrected capacity, current
    (598, 3),   # grid voltages L1-L3 (not live: nothing needs them every second)
    (613, 13),  # external CT currents and powers, power factor, grid side powers
    (627, 12),  # inverter output voltages, currents, powers, frequency
    (640, 10),  # UPS power, load voltages
    (661, 7),   # generator port voltages and powers
    (676, 8),   # PV1-4 voltage and current
]
SLOW: list[tuple[int, int]] = [
    (0, 8),     # device type, Modbus address, protocol version, inverter serial (ASCII)
    (10, 13),   # board and firmware versions, rated power, MPPTs and phases
    (36, 1),    # self-check
    (61, 4),    # self-check time, inverter clock
    (98, 16),   # battery settings
    (114, 16),  # battery SoC / voltage limits, generator and grid charging
    (130, 16),  # grid charge enable, generator, smart load, work mode, solar sell
    (146, 16),  # Time of Use, the six programs' times, powers, voltages
    (162, 16),  # the six programs' voltages, SoC and charge flags
    (178, 14),  # special functions, grid protection limits, peak shaving
    (210, 14),  # BMS limits and readings, alarm and fault words, BMS type
    (231, 7),   # max export / import power, AC coupling, export asymmetry
    (340, 2),   # PV power limit, MPPT scanning
    (500, 1),   # device state
]
# every register a LIVE block reads: a summary sensor of one of them is written every live interval
LIVE_REGS = frozenset(reg for start, count in LIVE for reg in range(start, start + count))
# every register a SLOW block reads: the settings and identity, written only after a settings poll
SLOW_REGS = frozenset(reg for start, count in SLOW for reg in range(start, start + count))

DEVICES = {
    0: "unknown", 2: "string inverter", 0x200: "string inverter",
    3: "single-phase hybrid", 0x300: "single-phase hybrid",
    4: "microinverter", 0x400: "microinverter",
    5: "LV 3-phase hybrid", 0x500: "LV 3-phase hybrid", 6: "HV 3-phase hybrid",
    7: "HV 3-phase inverter 6-15 kW", 0x600: "HV 3-phase inverter 6-15 kW",
    8: "HV 3-phase inverter 20-50 kW", 0x601: "HV 3-phase inverter 20-50 kW",
    0x103: "off-grid inverter", 0x104: "energy storage system",
}
ON_OFF = {0: "disabled", 1: "enabled"}
BATTERY_CONTROL = {0: "lead-acid", 1: "lithium"}
BATTERY_OPERATION = {0: "voltage", 1: "capacity", 2: "no battery"}
IO_MODES = {0: "generator", 1: "smart load", 2: "microinverter"}
GRID_VOLTAGES = {
    0: "LN 220 V / LL 380 V", 1: "LN 230 V / LL 398 V", 2: "LN 240 V / LL 415 V",
    3: "LN 120 V / LL 208 V", 4: "LN 133 V / LL 220 V", 5: "LN 100 V / LL 173 V",
}
CHARGING_SIGNALS = {0: "normal", 1: "grid", 2: "generator", 3: "both"}
ENERGY_PATTERNS = {0: "battery first", 1: "load first"}
GRID_FREQUENCIES = {0: "50 Hz", 1: "60 Hz"}
BMS_TYPES = {
    0: "PYLON", 1: "Tianbangda", 2: "KOK", 3: "Keith", 4: "Toppai", 5: "Peneng 485", 6: "Jeris 485",
    7: "Sunwoda 485", 8: "Xinrui 485", 9: "Tianbangda 485", 10: "Shenggao Electric CAN",
}
STATES = {0: "standby", 1: "self-test", 2: "normal", 3: "alarm", 4: "fault"}
WORK_MODES = {0: "selling first", 1: "zero export to load", 2: "zero export to CT"}
GRID_RELAY_BIT = 0x04  # 552: bit 2 = the grid relay is closed
GRID_PRESENT_MIN_V = 100  # an open relay with a phase at this voltage is still grid present


@dataclass(frozen=True)
class Value:
    key: str
    name: str
    reg: int
    unit: str | None = None
    scale: float = 1.0
    signed: bool = False
    words: int = 1  # 2 = 32 bit, low word first
    offset: int = 0  # temperatures: (raw - 1000) / 10
    options: dict[int, str] | None = None  # text instead of the number
    mask: int = 0  # only these bits, shifted down
    fmt: str | None = None  # "time" (HHMM), "hex" or "clock" (three registers from reg)
    high: int | None = None  # words=2: the high word's register, when it is not reg + 1


def _phases(key, name, first, unit, scale=1.0, signed=False, phases=3):
    return [Value(f"{key}_l{i}", f"{name} L{i}", first + i - 1, unit, scale, signed) for i in range(1, phases + 1)]


def _programs(key, name, first, unit=None, scale=1.0, fmt=None):
    return [Value(f"program_{i}_{key}", f"Program {i} {name}", first + i - 1, unit, scale, fmt=fmt) for i in range(1, 7)]


VALUES: list[Value] = [
    # identity / state
    Value("rated_power", "Rated power", 20, "W", 0.1, words=2),
    Value("device_state", "Device state", 500),
    Value("relay_status", "Relay status", 552),
    # PV
    Value("pv1_power", "PV1 power", 672, "W"),
    Value("pv2_power", "PV2 power", 673, "W"),
    Value("pv3_power", "PV3 power", 674, "W"),
    Value("pv4_power", "PV4 power", 675, "W"),
    Value("pv1_voltage", "PV1 voltage", 676, "V", 0.1),
    Value("pv1_current", "PV1 current", 677, "A", 0.1),
    Value("pv2_voltage", "PV2 voltage", 678, "V", 0.1),
    Value("pv2_current", "PV2 current", 679, "A", 0.1),
    # grid port (internal CTs): import > 0, export < 0
    *_phases("grid_voltage", "Grid voltage", 598, "V", 0.1),
    *_phases("grid_power", "Grid power", 604, "W", signed=True),
    Value("grid_power", "Grid power", 607, "W", signed=True),
    Value("grid_frequency", "Grid frequency", 609, "Hz", 0.01),
    *_phases("grid_current", "Grid current", 610, "A", 0.01, True),
    # inverter output
    *_phases("inverter_voltage", "Inverter voltage", 627, "V", 0.1),
    *_phases("inverter_current", "Inverter current", 630, "A", 0.01, True),
    *_phases("inverter_power", "Inverter power", 633, "W", signed=True),
    Value("inverter_power", "Inverter power", 636, "W", signed=True),
    Value("inverter_frequency", "Inverter frequency", 638, "Hz", 0.01),
    # load port (the whole house)
    Value("ups_power", "UPS load power", 643, "W", signed=True),
    *_phases("load_voltage", "Load voltage", 644, "V", 0.1),
    *_phases("load_power", "Load power", 650, "W", signed=True),
    Value("load_power", "Load power", 653, "W", signed=True),
    Value("load_frequency", "Load frequency", 655, "Hz", 0.01),
    # battery: discharge > 0, charge < 0
    Value("battery_temperature", "Battery temperature", 586, "°C", 0.1, offset=1000),
    Value("battery_voltage", "Battery voltage", 587, "V", 0.01),
    Value("battery_soc", "Battery SoC", 588, "%"),
    Value("battery_power", "Battery power", 590, "W", signed=True),
    Value("battery_current", "Battery current", 594, "A", 0.01, True),
    Value("dc_temperature", "DC temperature", 540, "°C", 0.1, offset=1000),
    Value("ac_temperature", "AC temperature", 541, "°C", 0.1, offset=1000),
    Value("bms_charge_voltage", "BMS charge voltage", 210, "V", 0.01),
    Value("bms_discharge_voltage", "BMS discharge voltage", 211, "V", 0.01),
    Value("bms_charge_current_limit", "BMS charge current limit", 212, "A"),
    Value("bms_discharge_current_limit", "BMS discharge current limit", 213, "A"),
    Value("bms_soc", "BMS SoC", 214, "%"),
    Value("bms_voltage", "BMS voltage", 215, "V", 0.01),
    Value("bms_current", "BMS current", 216, "A", signed=True),
    # energy (kWh)
    Value("daily_charge", "Battery charge today", 514, "kWh", 0.1),
    Value("daily_discharge", "Battery discharge today", 515, "kWh", 0.1),
    Value("total_charge", "Battery charge total", 516, "kWh", 0.1, words=2),
    Value("total_discharge", "Battery discharge total", 518, "kWh", 0.1, words=2),
    Value("daily_import", "Grid import today", 520, "kWh", 0.1),
    Value("daily_export", "Grid export today", 521, "kWh", 0.1),
    Value("total_import", "Grid import total", 522, "kWh", 0.1, words=2),
    Value("total_export", "Grid export total", 524, "kWh", 0.1, words=2),
    Value("daily_load", "Consumption today (inverter)", 526, "kWh", 0.1),
    Value("total_load", "Consumption total", 527, "kWh", 0.1, words=2),
    Value("daily_pv", "PV today", 529, "kWh", 0.1),
    Value("total_pv", "PV total", 534, "kWh", 0.1, words=2),
    # settings
    Value("battery_capacity", "Battery capacity", 102, "Ah"),
    Value("battery_low_soc", "Battery low SoC", 117, "%"),
    Value("grid_charge_current", "Grid charge current", 128, "A"),
    Value("grid_charge", "Grid charge enabled", 130),
    Value("work_mode", "Work mode", 142),
    Value("work_mode_name", "Work mode name", 142, options=WORK_MODES),  # the same register, as text
    Value("max_sell_power", "Max sell power", 143, "W"),
    Value("solar_sell", "Solar sell", 145),
    Value("time_of_use", "Time of use", 146),
    *_programs("soc", "SoC", 166, "%"),
    *_programs("charge", "charge", 172),
    # the rest of the map: the addresses, scales and texts of ha-solarman's deye_p3
    # identity
    Value("device_type", "Device type", 0, options=DEVICES),
    Value("modbus_address", "Modbus address", 1),
    Value("mcu_board_version", "MCU board version", 10, fmt="hex"),
    Value("afci_version", "AFCI version", 12, fmt="hex"),
    Value("mcu_version", "MCU version", 13, fmt="hex"),
    Value("mppts", "MPPTs", 22, mask=0x0F00),
    Value("phases", "Phases", 22, mask=0x000F),
    Value("self_check", "Self-check", 36, options=ON_OFF),
    Value("self_check_time", "Self-check time", 61, "s"),
    Value("inverter_clock", "Inverter clock", 62, fmt="clock"),
    # battery settings
    Value("battery_control_mode", "Battery control mode", 98, options=BATTERY_CONTROL),
    Value("battery_equalization_voltage", "Battery equalization voltage", 99, "V", 0.01),
    Value("battery_absorption_voltage", "Battery absorption voltage", 100, "V", 0.01),
    Value("battery_float_voltage", "Battery float voltage", 101, "V", 0.01),
    Value("battery_empty_voltage", "Battery empty voltage", 103, "V", 0.01),
    Value("zero_export_power", "Zero export power", 104, "W"),
    Value("battery_equalization_cycle", "Battery equalization cycle", 105, "d"),
    Value("battery_equalization_time", "Battery equalization time", 106, "h", 0.5),
    Value("battery_temperature_compensation", "Battery temperature compensation", 107, "mV/°C", signed=True),
    Value("battery_max_charge_current", "Battery max charge current", 108, "A"),
    Value("battery_max_discharge_current", "Battery max discharge current", 109, "A"),
    Value("battery_operation_mode", "Battery operation mode", 111, options=BATTERY_OPERATION),
    Value("battery_resistance", "Battery resistance", 113, "mΩ"),
    Value("battery_charge_efficiency", "Battery charge efficiency", 114, "%", 0.1),
    Value("battery_shutdown_soc", "Battery shutdown SoC", 115, "%"),
    Value("battery_restart_soc", "Battery restart SoC", 116, "%"),
    Value("battery_shutdown_voltage", "Battery shutdown voltage", 118, "V", 0.01),
    Value("battery_restart_voltage", "Battery restart voltage", 119, "V", 0.01),
    Value("battery_low_voltage", "Battery low voltage", 120, "V", 0.01),
    Value("generator_operating_time", "Generator operating time", 121, "h", 0.1),
    Value("generator_cooling_time", "Generator cooling time", 122, "h", 0.1),
    Value("generator_charge_start_voltage", "Generator charge start voltage", 123, "V", 0.01),
    Value("generator_charge_start_soc", "Generator charge start SoC", 124, "%"),
    Value("generator_charge_current", "Generator charge current", 125, "A"),
    Value("grid_charge_start_voltage", "Grid charge start voltage", 126, "V", 0.01),
    Value("grid_charge_start_soc", "Grid charge start SoC", 127, "%"),
    Value("generator_charge", "Generator charge enabled", 129),
    Value("generator_flag", "Generator flag", 132),
    Value("io_mode", "IO mode", 133, options=IO_MODES),
    Value("smart_load_off_voltage", "Smart load off voltage", 134, "V", 0.01),
    Value("smart_load_off_soc", "Smart load off SoC", 135, "%"),
    Value("smart_load_on_voltage", "Smart load on voltage", 136, "V", 0.01),
    Value("smart_load_on_soc", "Smart load on SoC", 137, "%"),
    # grid and work mode settings
    Value("grid_voltage_setting", "Grid voltage setting", 138, options=GRID_VOLTAGES),
    Value("generator_min_pv_power", "Generator min PV power", 139, "W"),
    Value("charging_signal", "Charging signal", 140, options=CHARGING_SIGNALS),
    Value("energy_pattern", "Energy pattern", 141, options=ENERGY_PATTERNS),
    *_programs("time", "time", 148, fmt="time"),
    *_programs("power", "power", 154, "W"),
    *_programs("voltage", "voltage", 160, "V", 0.01),
    Value("special_functions", "Special functions word", 178),
    Value("off_grid", "Off grid", 179),
    Value("grid_reconnection_time", "Grid reconnection time", 180, "s"),
    Value("grid_frequency_setting", "Grid frequency setting", 183, options=GRID_FREQUENCIES),
    Value("grid_protection_voltage_high", "Grid protection voltage high", 185, "V", 0.1),
    Value("grid_protection_voltage_low", "Grid protection voltage low", 186, "V", 0.1),
    Value("grid_protection_frequency_high", "Grid protection frequency high", 187, "Hz", 0.01),
    Value("grid_protection_frequency_low", "Grid protection frequency low", 188, "Hz", 0.01),
    Value("generator_grid_side", "Generator grid side", 189),
    Value("generator_peak_shaving_power", "Generator peak shaving power", 190, "W"),
    Value("grid_peak_shaving_power", "Grid peak shaving power", 191, "W"),
    Value("grid_max_export_power", "Grid max export power", 231, "W", 10.0),
    Value("grid_max_import_power", "Grid max import power", 233, "W", 10.0),
    Value("ac_coupling", "AC coupling word", 234),
    Value("export_asymmetry", "Export asymmetry", 237),
    Value("pv_power_limit", "PV power limit", 340, "W"),
    Value("mppt_scanning", "MPPT scanning word", 341),
    # BMS
    Value("bms_max_charge_current", "BMS max charge current", 218, "A"),
    Value("bms_max_discharge_current", "BMS max discharge current", 219, "A"),
    Value("bms_alarm", "BMS alarm word", 220),
    Value("bms_fault", "BMS fault word", 221),
    Value("bms_other", "BMS other word", 222),
    Value("bms_type", "BMS type", 223, options=BMS_TYPES),
    # energy and state
    Value("daily_generator", "Generator energy today", 536, "kWh", 0.1),
    Value("total_generator", "Generator energy total", 537, "kWh", 0.1, words=2),
    Value("relay_generator", "Generator relay", 552, mask=0x08),
    Value("device_alarm", "Alarm word", 553, words=2),
    Value("fault_1", "Fault word 1", 555, words=2),
    Value("fault_2", "Fault word 2", 557, words=2),
    # battery
    Value("battery_corrected_capacity", "Battery corrected capacity", 592, "Ah"),
    # external CTs and the grid side
    *_phases("external_ct_current", "External CT current", 613, "A", 0.01, True),
    *_phases("external_ct_power", "External CT power", 616, "W", signed=True),
    Value("external_ct_power", "External CT power", 619, "W", signed=True),
    Value("grid_power_factor", "Grid power factor", 621, "%", 0.1),
    *_phases("grid_side_power", "Grid side power", 622, "W", signed=True),
    Value("grid_side_power", "Grid side power", 625, "W", signed=True),
    # UPS, generator port, PV3/4
    *_phases("ups_power", "UPS load power", 640, "W", signed=True),
    *_phases("generator_voltage", "Generator voltage", 661, "V", 0.1),
    *_phases("generator_power", "Generator power", 664, "W", signed=True),
    Value("generator_power", "Generator power", 667, "W", signed=True),
    Value("pv3_voltage", "PV3 voltage", 680, "V", 0.1),
    Value("pv3_current", "PV3 current", 681, "A", 0.1),
    Value("pv4_voltage", "PV4 voltage", 682, "V", 0.1),
    Value("pv4_current", "PV4 current", 683, "A", 0.1),
]
BY_KEY = {v.key: v for v in VALUES}

# what the device page can change: every setting of the map, with the dangerous ones.  Left out: readings,
# identity, the clock (three registers) and the Modbus address (a new one ends the link).  The ranges fit the
# register, they do not protect anything.  A maximum is a number or RATED (what the inverter reports).
# _WORD is the whole 16-bit register: the raw words (bit fields included) and the values with no tighter range.
class _RatedPower:
    """Sentinel: use the inverter's own rated power (registers 20/21)."""


RATED = _RatedPower()
DEFAULT_RATED_W = 20000  # fallback when the inverter has not reported its rated power
_BATTERY_V = (0, 100, 0.01)
_SOC = (0, 100, 1)
_WORD = (0, 0xFFFF, 1)
_AMPS = (0, 2000, 1)
_POWER = (0, RATED, 100)
_HOURS = (0, 24, 0.1)
NUMBERS: dict[str, tuple[float, float | _RatedPower, float]] = {  # key: (min, max, step)
    "max_sell_power": _POWER,
    "zero_export_power": (0, 10000, 1),
    "battery_max_charge_current": _AMPS,
    "battery_max_discharge_current": _AMPS,
    "grid_charge_current": _AMPS,
    "grid_charge_start_soc": _SOC,
    "battery_low_soc": _SOC,
    **{f"program_{i}_power": _POWER for i in range(1, 7)},
    **{f"program_{i}_soc": _SOC for i in range(1, 7)},
    **{f"program_{i}_voltage": _BATTERY_V for i in range(1, 7)},
    # battery chemistry and voltages
    "battery_capacity": _WORD,
    "battery_equalization_voltage": _BATTERY_V,
    "battery_absorption_voltage": _BATTERY_V,
    "battery_float_voltage": _BATTERY_V,
    "battery_empty_voltage": _BATTERY_V,
    "battery_shutdown_voltage": _BATTERY_V,
    "battery_restart_voltage": _BATTERY_V,
    "battery_low_voltage": _BATTERY_V,
    "battery_equalization_cycle": (0, 365, 1),
    "battery_equalization_time": (0, 24, 0.5),
    "battery_temperature_compensation": (-100, 100, 1),
    "battery_resistance": _WORD,
    "battery_charge_efficiency": (0, 100, 0.1),
    "battery_shutdown_soc": _SOC,
    "battery_restart_soc": _SOC,
    "grid_charge_start_voltage": _BATTERY_V,
    # generator and smart load
    "generator_operating_time": _HOURS,
    "generator_cooling_time": _HOURS,
    "generator_charge_start_voltage": _BATTERY_V,
    "generator_charge_start_soc": _SOC,
    "generator_charge_current": _AMPS,
    "generator_min_pv_power": _POWER,
    "generator_peak_shaving_power": _POWER,
    "smart_load_off_voltage": _BATTERY_V,
    "smart_load_off_soc": _SOC,
    "smart_load_on_voltage": _BATTERY_V,
    "smart_load_on_soc": _SOC,
    # grid protection and limits
    "grid_protection_voltage_high": (0, 500, 0.1),
    "grid_protection_voltage_low": (0, 500, 0.1),
    "grid_protection_frequency_high": (0, 100, 0.01),
    "grid_protection_frequency_low": (0, 100, 0.01),
    "grid_reconnection_time": _WORD,
    "grid_peak_shaving_power": _POWER,
    "grid_max_export_power": (0, 655350, 10),
    "grid_max_import_power": (0, 655350, 10),
    "pv_power_limit": _WORD,
    "self_check_time": _WORD,
    # raw words
    "generator_charge": _WORD,
    "generator_flag": _WORD,
    "special_functions": _WORD,
    "off_grid": _WORD,
    "generator_grid_side": _WORD,
    "ac_coupling": _WORD,
    "export_asymmetry": _WORD,
    "mppt_scanning": _WORD,
}
SELECTS: dict[str, dict[int, str]] = {
    # these Values have no options: giving them some would change their detail sensors
    "work_mode": WORK_MODES,
    "time_of_use": {0: "disabled", 0xFF: "all week", 0x3F: "weekdays", 0xC1: "weekend"},
    **{f"program_{i}_charge": {0: "disabled", 1: "grid", 2: "generator", 3: "both"} for i in range(1, 7)},
    **{
        key: BY_KEY[key].options
        for key in (
            "energy_pattern", "battery_control_mode", "battery_operation_mode", "bms_type", "io_mode",
            "grid_voltage_setting", "grid_frequency_setting", "charging_signal", "self_check",
        )
    },
}
TIMES = [f"program_{i}_time" for i in range(1, 7)]
ON_OFF_SWITCHES = ["solar_sell"]

PROGRAM_CHARGE = BY_KEY["program_1_charge"].reg  # six consecutive registers
REG_GRID_CHARGE = BY_KEY["grid_charge"].reg
REG_RELAY = BY_KEY["relay_status"].reg
SERIAL_REG, SERIAL_WORDS = 3, 5
ALARM_REGS = range(553, 559)
DEVICE_TYPE_REG = BY_KEY["device_type"].reg
STATE = Value("state", "State", 500, options=STATES)
VERIFIED_DEVICE_TYPES = {5, 0x500}  # LV 3-phase hybrid


def _signed(raw: int) -> int:
    return raw - 0x10000 if raw >= 0x8000 else raw


def _words(regs: dict[int, int], addresses: Iterable[int]) -> list[int] | None:
    """These registers' words, None while one is missing."""
    words = [regs.get(reg) for reg in addresses]
    return None if None in words else words


def _decode_clock(regs: dict[int, int], first: int) -> str | None:
    """Three registers: year, month / day, hour / minute, second, one byte each."""
    words = _words(regs, (first, first + 1, first + 2))
    if words is None:
        return None
    a, b, c = words
    return f"20{a >> 8:02d}-{a & 0xFF:02d}-{b >> 8:02d} {b & 0xFF:02d}:{c >> 8:02d}:{c & 0xFF:02d}"


def decode_hhmm(raw: int | None) -> tuple[int, int] | None:
    """A time register (HHMM) as (hour, minute).  Not validated: 2460 is (24, 60); the time entity refuses it."""
    if raw is None:
        return None
    return raw // 100, raw % 100


def encode_hhmm(hour: int, minute: int) -> int:
    return hour * 100 + minute


def decode_value(v: Value, regs: dict[int, int]) -> float | int | str | None:
    """The value from the register words, None while a word is missing."""
    if v.fmt == "clock":
        return _decode_clock(regs, v.reg)
    low = regs.get(v.reg)
    if low is None:
        return None
    if v.fmt == "time":
        hour, minute = decode_hhmm(low)
        return f"{hour:02d}:{minute:02d}"
    if v.fmt == "hex":
        return f"{low:04X}"
    if v.options is not None:
        return v.options.get(low, f"unknown ({low})")
    if v.mask:
        return (low & v.mask) // (v.mask & -v.mask)
    if v.words == 2:
        high = regs.get(v.reg + 1 if v.high is None else v.high)
        if high is None:
            return None
        raw = (high << 16) | low
    else:
        raw = _signed(low) if v.signed else low
    value = (raw - v.offset) * v.scale
    return int(value) if v.scale == 1 and not v.offset else round(value, 3)


def encode_value(v: Value, value: float) -> int:
    """The register word of a number: the opposite of decode_value."""
    raw = round(value / v.scale) + v.offset
    low, high = (-0x8000, 0x7FFF) if v.signed else (0, 0xFFFF)
    if not low <= raw <= high:
        raise ValueError(f"{v.key}: {value} is register word {raw}")
    return raw & 0xFFFF


def decode_serial(regs: dict[int, int]) -> str | None:
    words = _words(regs, range(SERIAL_REG, SERIAL_REG + SERIAL_WORDS))
    if words is None:
        return None
    text = b"".join(w.to_bytes(2, "big") for w in words).decode("ascii", "replace")
    return text.strip("\x00 ") or None


# firmware as the Deye app shows it: hex words joined by "-"
FIRMWARE = {"main": (14, 15, 11), "hmi": (17, 18), "protocol": (2,)}


def decode_firmware(regs: dict[int, int], part: str, m: RegisterMap | None = None) -> str | None:
    words = _words(regs, (m or THREE_PHASE).firmware[part])
    return None if words is None else "-".join(f"{w:04X}" for w in words)


def rated_power(regs: dict[int, int], m: RegisterMap | None = None) -> float | None:
    return decode_value((m or THREE_PHASE).by_key["rated_power"], regs)


def device_model(regs: dict[int, int], m: RegisterMap | None = None) -> str | None:
    """As far as the inverter says: its type and rated power."""
    kind = decode_value(BY_KEY["device_type"], regs)
    rated = rated_power(regs, m)
    if kind is None:
        return None
    return f"{kind}, {rated / 1000:g} kW" if rated else kind


def pv_power(regs: dict[int, int], m: RegisterMap | None = None) -> int | None:
    by_key = (m or THREE_PHASE).by_key
    parts = [decode_value(by_key[f"pv{i}_power"], regs) for i in (1, 2, 3, 4)]
    return None if any(p is None for p in parts) else sum(parts)


@dataclass(frozen=True, eq=False)
class RegisterMap:
    """One inverter family's registers: the poll blocks, the values and the controls of the device page."""

    name: str
    live: list[tuple[int, int]]
    fast: list[tuple[int, int]]
    slow: list[tuple[int, int]]
    values: list[Value]
    numbers: dict[str, tuple[float, float | _RatedPower, float]]
    selects: dict[str, dict[int, str]]
    firmware: dict[str, tuple[int, ...]]
    state: Value
    alarm_regs: range
    relay_reg: int
    relay_mask: int  # the bits of relay_reg that say the grid relay is closed
    phases: int  # the grid phases the summary sensors show
    verified: bool  # checked on a real logger
    times: list[str] = field(default_factory=lambda: list(TIMES))
    on_off_switches: list[str] = field(default_factory=lambda: list(ON_OFF_SWITCHES))
    by_key: dict[str, Value] = field(init=False)
    live_regs: frozenset[int] = field(init=False)
    slow_regs: frozenset[int] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_key", {v.key: v for v in self.values})
        object.__setattr__(self, "live_regs", frozenset(reg for s, c in self.live for reg in range(s, s + c)))
        object.__setattr__(self, "slow_regs", frozenset(reg for s, c in self.slow for reg in range(s, s + c)))

    @property
    def program_charge(self) -> int:
        return self.by_key["program_1_charge"].reg  # six consecutive registers

    @property
    def grid_charge_reg(self) -> int:
        return self.by_key["grid_charge"].reg

    @property
    def grid_voltages(self) -> list[Value]:
        """The grid phase voltages: the grid sensor is on while one of them is up."""
        return [v for v in self.values if v.key.startswith("grid_voltage_l")]


THREE_PHASE = RegisterMap(
    name="3-phase LV hybrid (deye_p3)", live=LIVE, fast=FAST, slow=SLOW, values=VALUES, numbers=NUMBERS,
    selects=SELECTS, firmware=FIRMWARE, state=STATE, alarm_regs=ALARM_REGS, relay_reg=REG_RELAY,
    relay_mask=GRID_RELAY_BIT, phases=3, verified=True,
)

# The single-phase hybrid map (SUN-*K-SG0*LP1): the addresses, scales and texts of ha-solarman's deye_hybrid
# (Deye MODBUS RTU V118).  L2 is the second leg of the split-phase (US) models; 0 elsewhere.  The keys are the
# 3-phase map's where the meaning is the same, so the summary sensors and the controls keep their ids.
_1P_LIVE: list[tuple[int, int]] = [
    (160, 16),  # grid, external CT and inverter currents, generator power, grid / CT / inverter powers
    (176, 15),  # load powers and currents, generator voltage, battery temperature, voltage, SoC, PV1-4 power,
                # battery power
]
_1P_FAST: list[tuple[int, int]] = [
    (70, 16),   # daily / total battery, grid and load energy, grid frequency
    (86, 12),   # total load energy, DC / AC temperature, total PV
    (101, 16),  # alarm and fault words, daily PV, PV1-4 voltage and current
    (150, 10),  # grid, inverter and load voltages
    (191, 4),   # battery current, load and inverter frequency, grid relay
]
_1P_SLOW: list[tuple[int, int]] = [
    (0, 8),     # device type, Modbus address, protocol version, inverter serial (ASCII)
    (10, 15),   # firmware versions, rated power, MPPTs and phases, self-check time, inverter clock
    (59, 1),    # device state
    (200, 16),  # battery settings
    (216, 16),  # battery SoC / voltage limits, generator and grid charging
    (232, 16),  # grid charge enable, smart load, work mode, solar sell
    (248, 16),  # Time of Use, the six programs' times, powers, voltages
    (264, 16),  # the six programs' voltages, SoC and charge flags
    (280, 14),  # special functions, grid protection limits, peak shaving
    (312, 15),  # BMS limits and readings, alarm and fault words, BMS type, AC coupling
]
_1P_VALUES: list[Value] = [
    # identity / state
    Value("rated_power", "Rated power", 16, "W", 0.1, words=2),
    Value("device_state", "Device state", 59),
    Value("relay_status", "Relay status", 194),
    Value("device_type", "Device type", 0, options=DEVICES),
    Value("modbus_address", "Modbus address", 1),
    Value("mppts", "MPPTs", 18, mask=0x0F00),
    Value("phases", "Phases", 18, mask=0x000F),
    Value("self_check_time", "Self-check time", 21, "s"),
    Value("inverter_clock", "Inverter clock", 22, fmt="clock"),
    # PV
    Value("pv1_power", "PV1 power", 186, "W"),
    Value("pv2_power", "PV2 power", 187, "W"),
    Value("pv3_power", "PV3 power", 188, "W"),
    Value("pv4_power", "PV4 power", 189, "W"),
    Value("pv1_voltage", "PV1 voltage", 109, "V", 0.1),
    Value("pv1_current", "PV1 current", 110, "A", 0.1),
    Value("pv2_voltage", "PV2 voltage", 111, "V", 0.1),
    Value("pv2_current", "PV2 current", 112, "A", 0.1),
    Value("pv3_voltage", "PV3 voltage", 113, "V", 0.1),
    Value("pv3_current", "PV3 current", 114, "A", 0.1),
    Value("pv4_voltage", "PV4 voltage", 115, "V", 0.1),
    Value("pv4_current", "PV4 current", 116, "A", 0.1),
    # grid port (internal CTs): import > 0, export < 0
    *_phases("grid_voltage", "Grid voltage", 150, "V", 0.1, phases=2),
    Value("grid_voltage", "Grid voltage", 152, "V", 0.1),
    *_phases("grid_current", "Grid current", 160, "A", 0.01, True, phases=2),
    *_phases("grid_power", "Grid power", 167, "W", signed=True, phases=2),
    Value("grid_power", "Grid power", 169, "W", signed=True),
    Value("grid_frequency", "Grid frequency", 79, "Hz", 0.01),
    *_phases("external_ct_current", "External CT current", 162, "A", 0.01, True, phases=2),
    *_phases("external_ct_power", "External CT power", 170, "W", signed=True, phases=2),
    Value("external_ct_power", "External CT power", 172, "W", signed=True),
    # inverter output
    *_phases("inverter_voltage", "Inverter voltage", 154, "V", 0.1, phases=2),
    Value("inverter_voltage", "Inverter voltage", 156, "V", 0.1),
    *_phases("inverter_current", "Inverter current", 164, "A", 0.01, True, phases=2),
    *_phases("inverter_power", "Inverter power", 173, "W", signed=True, phases=2),
    Value("inverter_power", "Inverter power", 175, "W", signed=True),
    Value("inverter_frequency", "Inverter frequency", 193, "Hz", 0.01),
    # load port (the whole house)
    *_phases("load_voltage", "Load voltage", 157, "V", 0.1, phases=2),
    *_phases("load_power", "Load power", 176, "W", signed=True, phases=2),
    Value("load_power", "Load power", 178, "W", signed=True),
    *_phases("load_current", "Load current", 179, "A", 0.01, True, phases=2),
    Value("load_frequency", "Load frequency", 192, "Hz", 0.01),
    # generator port
    Value("generator_power", "Generator power", 166, "W"),
    Value("generator_voltage", "Generator voltage", 181, "V", 0.1),
    # battery: discharge > 0, charge < 0
    Value("battery_temperature", "Battery temperature", 182, "°C", 0.1, offset=1000),
    Value("battery_voltage", "Battery voltage", 183, "V", 0.01),
    Value("battery_soc", "Battery SoC", 184, "%"),
    Value("battery_power", "Battery power", 190, "W", signed=True),
    Value("battery_current", "Battery current", 191, "A", 0.01, True),
    Value("dc_temperature", "DC temperature", 90, "°C", 0.1, offset=1000),
    Value("ac_temperature", "AC temperature", 91, "°C", 0.1, offset=1000),
    # energy (kWh)
    Value("daily_charge", "Battery charge today", 70, "kWh", 0.1),
    Value("daily_discharge", "Battery discharge today", 71, "kWh", 0.1),
    Value("total_charge", "Battery charge total", 72, "kWh", 0.1, words=2),
    Value("total_discharge", "Battery discharge total", 74, "kWh", 0.1, words=2),
    Value("daily_import", "Grid import today", 76, "kWh", 0.1),
    Value("daily_export", "Grid export today", 77, "kWh", 0.1),
    Value("total_import", "Grid import total", 78, "kWh", 0.1, words=2, high=80),  # 79 is the grid frequency
    Value("total_export", "Grid export total", 81, "kWh", 0.1, words=2),
    Value("daily_load", "Consumption today (inverter)", 84, "kWh", 0.1),
    Value("total_load", "Consumption total", 85, "kWh", 0.1, words=2),
    Value("daily_pv", "PV today", 108, "kWh", 0.1),
    Value("total_pv", "PV total", 96, "kWh", 0.1, words=2),
    Value("device_alarm", "Alarm word", 101, words=2),
    Value("fault_1", "Fault word 1", 103, words=2),
    Value("fault_2", "Fault word 2", 105, words=2),
    # battery settings
    Value("battery_control_mode", "Battery control mode", 200, options=BATTERY_CONTROL),
    Value("battery_equalization_voltage", "Battery equalization voltage", 201, "V", 0.01),
    Value("battery_absorption_voltage", "Battery absorption voltage", 202, "V", 0.01),
    Value("battery_float_voltage", "Battery float voltage", 203, "V", 0.01),
    Value("battery_capacity", "Battery capacity", 204, "Ah"),
    Value("battery_empty_voltage", "Battery empty voltage", 205, "V", 0.01),
    Value("zero_export_power", "Zero export power", 206, "W", signed=True),
    Value("battery_equalization_cycle", "Battery equalization cycle", 207, "d"),
    Value("battery_equalization_time", "Battery equalization time", 208, "h", 0.5),
    Value("battery_temperature_compensation", "Battery temperature compensation", 209, "mV/°C", signed=True),
    Value("battery_max_charge_current", "Battery max charge current", 210, "A"),
    Value("battery_max_discharge_current", "Battery max discharge current", 211, "A"),
    Value("battery_operation_mode", "Battery operation mode", 213, options=BATTERY_OPERATION),
    Value("battery_resistance", "Battery resistance", 215, "mΩ"),
    Value("battery_charge_efficiency", "Battery charge efficiency", 216, "%", 0.1),
    Value("battery_shutdown_soc", "Battery shutdown SoC", 217, "%"),
    Value("battery_restart_soc", "Battery restart SoC", 218, "%"),
    Value("battery_low_soc", "Battery low SoC", 219, "%"),
    Value("battery_shutdown_voltage", "Battery shutdown voltage", 220, "V", 0.01),
    Value("battery_restart_voltage", "Battery restart voltage", 221, "V", 0.01),
    Value("battery_low_voltage", "Battery low voltage", 222, "V", 0.01),
    Value("generator_operating_time", "Generator operating time", 223, "h", 0.1),
    Value("generator_cooling_time", "Generator cooling time", 224, "h", 0.1),
    Value("generator_charge_start_voltage", "Generator charge start voltage", 225, "V", 0.01),
    Value("generator_charge_start_soc", "Generator charge start SoC", 226, "%"),
    Value("generator_charge_current", "Generator charge current", 227, "A"),
    Value("grid_charge_start_voltage", "Grid charge start voltage", 228, "V", 0.01),
    Value("grid_charge_start_soc", "Grid charge start SoC", 229, "%"),
    Value("grid_charge_current", "Grid charge current", 230, "A"),
    Value("generator_charge", "Generator charge enabled", 231),
    Value("grid_charge", "Grid charge enabled", 232),
    Value("io_mode", "IO mode", 235, options=IO_MODES),
    Value("smart_load_off_voltage", "Smart load off voltage", 236, "V", 0.01),
    Value("smart_load_off_soc", "Smart load off SoC", 237, "%"),
    Value("smart_load_on_voltage", "Smart load on voltage", 238, "V", 0.01),
    Value("smart_load_on_soc", "Smart load on SoC", 239, "%"),
    # grid and work mode settings
    Value("generator_min_pv_power", "Generator min PV power", 241, "W"),
    Value("charging_signal", "Charging signal", 242, options=CHARGING_SIGNALS),
    Value("energy_pattern", "Energy pattern", 243, options=ENERGY_PATTERNS),
    Value("work_mode", "Work mode", 244),
    Value("work_mode_name", "Work mode name", 244, options=WORK_MODES),  # the same register, as text
    Value("max_sell_power", "Max sell power", 245, "W"),
    Value("solar_sell", "Solar sell", 247),
    Value("time_of_use", "Time of use", 248),
    *_programs("time", "time", 250, fmt="time"),
    *_programs("power", "power", 256, "W"),
    *_programs("voltage", "voltage", 262, "V", 0.01),
    *_programs("soc", "SoC", 268, "%"),
    *_programs("charge", "charge", 274),
    Value("special_functions", "Special functions word", 280),
    Value("off_grid", "Off grid", 281),
    Value("grid_reconnection_time", "Grid reconnection time", 282, "s"),
    Value("grid_frequency_setting", "Grid frequency setting", 285, options=GRID_FREQUENCIES),
    Value("grid_protection_voltage_high", "Grid protection voltage high", 287, "V", 0.1),
    Value("grid_protection_voltage_low", "Grid protection voltage low", 288, "V", 0.1),
    Value("grid_protection_frequency_high", "Grid protection frequency high", 289, "Hz", 0.01),
    Value("grid_protection_frequency_low", "Grid protection frequency low", 290, "Hz", 0.01),
    Value("generator_grid_side", "Generator grid side", 291),
    Value("generator_peak_shaving_power", "Generator peak shaving power", 292, "W"),
    Value("grid_peak_shaving_power", "Grid peak shaving power", 293, "W"),
    # BMS
    Value("bms_charge_voltage", "BMS charge voltage", 312, "V", 0.01),
    Value("bms_discharge_voltage", "BMS discharge voltage", 313, "V", 0.01),
    Value("bms_charge_current_limit", "BMS charge current limit", 314, "A"),
    Value("bms_discharge_current_limit", "BMS discharge current limit", 315, "A"),
    Value("bms_soc", "BMS SoC", 316, "%"),
    Value("bms_voltage", "BMS voltage", 317, "V", 0.01),
    Value("bms_current", "BMS current", 318, "A", signed=True),
    Value("bms_max_charge_current", "BMS max charge current", 320, "A"),
    Value("bms_max_discharge_current", "BMS max discharge current", 321, "A"),
    Value("bms_alarm", "BMS alarm word", 322),
    Value("bms_fault", "BMS fault word", 323),
    Value("bms_other", "BMS other word", 324),
    Value("bms_type", "BMS type", 325, options=BMS_TYPES),
    Value("ac_coupling", "AC coupling word", 326),
]
_1P_KEYS = {v.key for v in _1P_VALUES}
SINGLE_PHASE = RegisterMap(
    name="single-phase hybrid (deye_hybrid)", live=_1P_LIVE, fast=_1P_FAST, slow=_1P_SLOW, values=_1P_VALUES,
    numbers={key: limits for key, limits in NUMBERS.items() if key in _1P_KEYS},
    selects={key: options for key, options in SELECTS.items() if key in _1P_KEYS},
    firmware={"main": (13, 12, 11), "hmi": (14, 10), "protocol": (2,)},  # control board, communication board
    state=Value("state", "State", 59, options=STATES), alarm_regs=range(101, 107),
    relay_reg=194, relay_mask=0xFFFF,  # 194 is the grid relay as a whole: non-zero = closed
    phases=1, verified=False,
)
SINGLE_PHASE_TYPES = {3, 0x300}


def map_for(device_type: int | None) -> RegisterMap:
    """The map of this device type (register 0).  Anything not single-phase gets the 3-phase map, as before."""
    return SINGLE_PHASE if device_type in SINGLE_PHASE_TYPES else THREE_PHASE

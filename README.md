# Deye Bluetooth for Home Assistant

[![Validate](https://github.com/csorbalazs306/ha-deye-bluetooth/actions/workflows/validate.yml/badge.svg)](https://github.com/csorbalazs306/ha-deye-bluetooth/actions/workflows/validate.yml)
[![Tests](https://github.com/csorbalazs306/ha-deye-bluetooth/actions/workflows/tests.yml/badge.svg)](https://github.com/csorbalazs306/ha-deye-bluetooth/actions/workflows/tests.yml)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://hacs.xyz/docs/faq/custom_repositories/)

Reads and controls a Deye hybrid inverter locally, through the Bluetooth console of its
Wi-Fi/BLE logger. No cloud, no extra wiring, no Modbus adapter.

This is an independent project, not made by or affiliated with Deye.

## Features

- **Live data**: grid phases and load every second, everything else every 15 s (configurable).
- **Every decoded register** as a sensor: PV, battery, BMS, grid, inverter, generator, energy counters, faults.
- **Settings on the device page**: Time-of-Use programs, work mode, charge limits, solar sell.
- **Grid-charge switch** and a **grid-present** binary sensor.
- **Discovered automatically** over Bluetooth, set up in the UI.
- **Several loggers** on one Home Assistant.
- **Safe by default**: read-only until you turn on *Allow writes*.

## Compatibility

| | Status |
|---|---|
| SUN-15K-SG05LP3-EU-SM2 with logger `DYDA_WiBLE_1.6.2` | **Verified**: reads and writes |
| Other Deye 3-phase LV hybrids | Untested, likely to read correctly |
| Other Deye models (single-phase, HV) | Untested: registers may differ, a warning is logged |

Reports from other models are welcome, see [CONTRIBUTING](CONTRIBUTING.md).

## Requirements

- Home Assistant 2026.3 or newer.
- A Bluetooth adapter or an [ESPHome Bluetooth proxy](https://esphome.io/components/bluetooth_proxy.html) with an
  active connection within range of the logger. Signal quality matters more than anything else here.
- The Deye phone app closed: the logger accepts one Bluetooth connection at a time.

## Installation

### HACS

[![Open in HACS](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=csorbalazs306&repository=ha-deye-bluetooth&category=integration)

Or add `https://github.com/csorbalazs306/ha-deye-bluetooth` as a custom repository of type
*Integration*, download **Deye Bluetooth**, and restart Home Assistant.

### Manually

Copy `custom_components/deye_bluetooth` into your `config/custom_components` folder and restart.

## Setup

The logger shows up under **Settings → Devices & services** once it is in range. Otherwise use
**Add integration → Deye Bluetooth**.

Rated power and device type come from the inverter. Everything else is on the options page (the
integration's **Configure** button):

| Section | Option | Default |
|---|---|---|
| Polling | Live interval (grid phases and load, each change is a recorder row) | 1 s |
| | Poll interval (everything else) | 15 s |
| | Settings interval | 300 s |
| | Values / settings go unavailable after | 120 s / 900 s |
| Writing | Allow writes | off |
| | Write attempts | 3 |
| Limits | Maximum battery current | 350 A |
| | Maximum zero export power | 500 W |
| | Power step | 100 W |
| | Lowest allowed low SoC | 10 % |
| | Grid present above (with the relay open) | 100 V |
| Bluetooth link | Modbus address | 1 |
| | Registers per read (longer reads are split) | 16 |
| | Reply timeout, connect attempts | 10 s, 3 |
| | Write and notify characteristic UUIDs | see [protocol](docs/protocol.md) |

The register map and the discovery UUID are in the code: the inverter cannot report the first, and Home Assistant
needs the second in `manifest.json`. **Check the limits against your inverter and battery manual before allowing writes:
the defaults fit the verified model.**

## Entities

The first logger gets the prefix `deye`, the second `deye_2`, and so on.

| Entity | Meaning |
|---|---|
| `sensor.deye_grid_power` | Grid power (W): import > 0, export < 0 |
| `sensor.deye_grid_phase_{a,b,c}_{current,voltage,power}` | Per phase (power in kW) |
| `sensor.deye_load_power` | Load (kW) |
| `sensor.deye_pv_power`, `sensor.deye_pv{1,2}_power` | PV (W) |
| `sensor.deye_battery_{soc,power,capacity}` | Battery: discharge > 0, charge < 0 |
| `sensor.deye_{grid_import,grid_export,load,pv}_energy` | Totals (kWh), for the Energy dashboard |
| `binary_sensor.deye_grid` | The grid is present |
| `switch.deye_grid_charge` | Battery charges from the grid, see [below](#grid-charge-switch) |
| `sensor.deye_inverter_<key>` | Every decoded register |
| `number.`, `select.`, `time.`, `switch.deye_inverter_<key>` | The settings |

Grid protection, battery chemistry and voltages stay out of the controls on purpose.
Power limits follow the rated power the inverter reports.

### Grid-charge switch

`switch.deye_grid_charge` writes seven registers and nothing else: the grid flag of all six Time-of-Use programs
and the inverter's Grid Charge enable.

- **On** sets the flag on every program and enables Grid Charge. Each program charges up to its own SoC, so set
  `number.deye_inverter_program_<n>_soc` to how far the battery should charge.
- **Off** clears the flag on every program and disables Grid Charge. The battery still covers the house only down to
  each program's SoC.

The switch never changes a program's SoC. It does overwrite the grid flags: if only some of your programs charge from
the grid, the first toggle makes all six the same. A program's generator flag stays as it is. The switch shows on when
Grid Charge and program 1's flag are both set.

## Actions

| Action | Fields |
|---|---|
| `deye_bluetooth.read_registers` | `address`, `count` (1–125), returns the raw words |
| `deye_bluetooth.write_register` | `address`, `value` (a number or a list). Admin users only |

With more than one logger, both also need `config_entry_id`.

## ⚠️ Writing to the inverter

Writes change a device connected to mains and a battery. **A wrong value can stop the
inverter, damage the battery, or break grid rules. You use this at your own risk.**

Writes are off by default. When on, only changed registers are written (they live in EEPROM)
and each one is read back; a mismatch is an error. `write_register` accepts **any** register,
so only admin users can call it, and you should check the address against your model's map first.

## Troubleshooting

- **Not discovered**: close the Deye app, and check that the adapter sees the logger under the Bluetooth integration.
- **Keeps dropping**: move the adapter or a proxy closer. Weak signal is the usual cause.
- **Grid-charge switch fails**: turn on *Allow writes*.
- **Debug logs**: add `custom_components.deye_bluetooth: debug` under `logger:`.
- **Reporting**: attach the device's diagnostics. They leave out the serial number and Bluetooth address.

## Credits

The logger protocol was first documented by [PetePeter/deye-bluetooth](https://github.com/PetePeter/deye-bluetooth)
(MIT), the register map follows [ha-solarman](https://github.com/davidrapan/ha-solarman). See [docs/protocol.md](docs/protocol.md).

## License

[MIT](LICENSE)

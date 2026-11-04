# Changelog

## 1.2.0

- Every setting of the register map is now a control: grid protection, grid standard, battery chemistry and voltages,
  generator, smart load, AC coupling and peak shaving. **These write without safety checks, once *Allow writes* is on, and
  most are not tried on real hardware.** Read the extended disclaimer in the README.
- Signed settings accept negative values (battery temperature compensation).
- README: tidier page, HACS default install.

## 1.1.0

- The grid-charge switch writes only the six programs' grid flags and the Grid Charge enable. It no longer changes a
  program's SoC, so each program charges up to its own SoC, and the *Grid-charge target SoC* option is gone.
  **Check your program SoC values after updating:** earlier versions left all six at the target SoC or the low SoC,
  whichever the last flip wrote. The battery covers the house only down to a program's SoC, so a program left at 100 %
  holds it full.
- `deye_bluetooth.write_register` can only be called by an admin user.

## 1.0.1

- Adding a logger by hand no longer aborts while a discovery of the same logger is waiting.
- Writes confirmed on real hardware (written, read back, restored).
- Nothing fixed that can vary: limits, thresholds, intervals, the Modbus address, the read size, timeouts and
  characteristic UUIDs are on the options page, with the old values as defaults.
- The grid-charge switch no longer guesses a low SoC: it refuses to turn off when the inverter's is not known.
- `read_registers` reads up to 125 registers, split as the logger needs.

## 1.0.0

First public release, extracted from a private Home Assistant setup where it has
been running against a SUN-15K-SG05LP3-EU-SM2.

- Summary sensors, every decoded register, and settings controls on the device page.
- Writes are off by default; every write is read back.
- Several loggers: entity prefixes `deye`, `deye_2`, ...; actions take `config_entry_id`.
- Power limits and the device model come from the inverter.
- Diagnostics download without the serial number and Bluetooth address.

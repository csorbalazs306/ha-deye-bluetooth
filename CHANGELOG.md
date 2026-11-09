# Changelog

## 1.4.0

- **Alert when the logger stops answering:** after 5 min without an answer a Repairs issue appears, and it goes away by
  itself once the logger answers. The logger has no pairing or password and takes one connection at a time, so besides
  the phone app and a weak signal, a long silence can mean that another device in Bluetooth range holds it. The delay
  is the new *Alert after no answer* option in the Polling section; 0 turns the alert off. It also works for a logger
  that never answered since Home Assistant started.
- A late disconnect of a replaced connection no longer leaves the new connection open but unused. The logger takes one
  connection, so that leftover one blocked the next poll.
- The `read_registers` and `write_register` actions refuse a register range past 65535 with a clear error, not an
  internal one.
- After the logger was out of range, the first failed connection waits 2 s again, not up to 60 s.
- The options page refuses a *Values go unavailable after* shorter than the live interval, which made the live values
  unavailable between polls.

## 1.3.1

- **New installs get fewer entities:** the 113 `sensor.deye_inverter_<key>` sensors that repeat a control or a summary
  sensor are disabled by default. Enable one on the device page if you need it. Existing entities are not changed.
- A write reads back only the poll blocks it touched instead of all 26, and the grid-charge switch asks for one poll
  instead of two. A side effect of a write on another register now shows at that register's next scheduled poll.
- After a failed connection the polls wait 2, 4, 8 ... up to 60 s before connecting again, so a failing logger no longer
  competes with the phone app every second. A logger out of range is still looked for at every poll, and the actions
  do not wait.
- A register the inverter refuses no longer drops the link: `read_registers` and the writes fail with the inverter's
  answer and the link stays up, and a refused write is not retried.
- The characteristic search gives each pair at most 2 s, saves the pair it finds in the options so it runs once, and a
  failed search no longer leaves the last tried pair as the configured one.
- Settings entities are written after a settings poll instead of every 15 s, unless their availability changes, and
  availability is no longer recomputed for every entity every second.
- Grid voltages are read every 15 s instead of every second (their entities were already written every 15 s), registers
  601-603 are no longer read, and load frequency no longer takes its own read.
- `sensor.deye_inverter_work_mode_name` is `unavailable` instead of `unknown` while its register is missing.
- The actions use Home Assistant's own entry lookup: its error texts apply, and with several loggers configured
  `config_entry_id` is required even if only one of them is loaded.
- Renaming an entry no longer reloads it; saving the options still does.

## 1.3.0

- A logger without the configured write or notify characteristic is no longer a dead end: the pair that answers the
  handshake is found on connect and logged as a warning. The service UUID stays fixed, discovery needs it.
- The entry migration the 1.2.0 notes describe ships here: stale *Limits* options are removed on upgrade.
- Power limits fall back to 20000 W until the inverter has reported its rated power, instead of a maximum of 0.
- The options page rejects fractions in whole-number fields and no longer fails on missing or malformed values.
- Tests and Validate also run on published releases and on demand.

## 1.2.0

- **Breaking change — check your settings before allowing writes:** the old *Limits* options (maximum battery
  current, maximum zero export power, power step, lowest allowed low SoC, grid present above) no longer exist. On
  upgraded entries their stored values are ignored and removed by the entry migration. The number entities now use
  the register's own range (battery low SoC 0–100 %, currents 0–2000 A, zero export 0–10000 W) and the
  grid-present threshold is a fixed 100 V, not a device-page control. **Your previous custom caps no longer apply
  and the wider ranges do not protect anything — re-check every setting against your inverter and battery manual.**
  Power limits still follow the rated power the inverter reports.
- Every setting of the register map is now a control: grid protection, grid standard, battery chemistry and voltages,
  generator, smart load, AC coupling and peak shaving. **These write without safety checks, once *Allow writes* is on, and
  most are not tried on real hardware.** Read the extended disclaimer in the README.
- Signed settings accept negative values (battery temperature compensation).
- The options page keeps only polling, write safety and the Bluetooth link; the inverter's own settings are controls
  under **Configuration** on the device page.
- Every numeric option is an input box, not a slider (also the `read_registers` count field).
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

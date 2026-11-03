# Changelog

## 1.0.1

- Adding a logger by hand no longer aborts while a discovery of the same logger is waiting.
- Writes confirmed on real hardware (written, read back, restored).

## 1.0.0

First public release, extracted from a private Home Assistant setup where it has
been running against a SUN-15K-SG05LP3-EU-SM2.

- Summary sensors, every decoded register, and settings controls on the device page.
- Writes are off by default; every write is read back.
- Several loggers: entity prefixes `deye`, `deye_2`, ...; actions take `config_entry_id`.
- Power limits and the device model come from the inverter.
- Diagnostics download without the serial number and Bluetooth address.

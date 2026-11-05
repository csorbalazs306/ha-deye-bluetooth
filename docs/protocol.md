# The logger's Bluetooth protocol

Measured on a `DYDA_WiBLE_1.6.2` logger on a SUN-15K-SG05LP3-EU-SM2. Protocol first
documented by [PetePeter/deye-bluetooth](https://github.com/PetePeter/deye-bluetooth).

| Item | Value |
|------|-------|
| Service UUID | `00000922-0000-1000-8000-00805f9b34fb` |
| Write (with response) | `0000fec7-0000-1000-8000-00805f9b34fb` |
| Notify | `0000fed8-0000-1000-8000-00805f9b34fb` |
| Handshake | `AT+DTYPE\n` → `+ok=21521,21521` |
| Read | `AT+INVDATA=8,<Modbus RTU hex>\n` → `+ok=<response hex>` |
| Write one register | `AT+INVDATA=11,<function 0x10 hex>\n` → `+ok=<ack hex>` |

Frames are hex, slave 1, CRC-16/Modbus low byte first.

- The logger takes one central at a time and stops advertising while connected.
- Reads of more than 16 registers go unanswered and drop the link.
- One command takes about 0.3 s.
- If a logger lacks the configured write or notify characteristic, the integration tries each write and notify pair
  it has until one answers the handshake. The service UUID stays fixed: discovery needs it before any connection.

The register map follows the Deye 3-phase LV hybrid map (MODBUS RTU V105, the
[ha-solarman](https://github.com/davidrapan/ha-solarman) `deye_p3` profile).
Scales and addresses are in
[`registers.py`](../custom_components/deye_bluetooth/registers.py).

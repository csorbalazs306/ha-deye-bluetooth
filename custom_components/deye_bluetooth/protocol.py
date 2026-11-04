"""The Deye Wi-Fi/BLE logger's Bluetooth console: framing only, no I/O.

The logger (DYDA_WiBLE_1.6.2) passes Modbus RTU frames to the inverter through
an AT command on its GATT console (protocol after PetePeter/deye-bluetooth,
confirmed on a real logger):

    AT+DTYPE\\n                        -> +ok=21521,21521   (handshake)
    AT+INVDATA=8,<func 03 frame>\\n     -> +ok=<response>    (read)
    AT+INVDATA=11,<func 10 frame>\\n    -> +ok=<ack>         (write one register)

Frames are hex, slave 1, CRC-16/Modbus low byte first.  Kept free of Home
Assistant imports so the tests run without it.
"""
from __future__ import annotations

import re

SLAVE = 1  # default, see options.py
WRITE_CHAR = "0000fec7-0000-1000-8000-00805f9b34fb"  # default: write with response
NOTIFY_CHAR = "0000fed8-0000-1000-8000-00805f9b34fb"  # default
MODBUS_MAX_READ = 125  # the Modbus limit
READ, WRITE = 0x03, 0x10
MAX_READ = 16  # default: this logger does not answer a longer read and drops the link

_HANDSHAKE = re.compile(r"\+ok=\d+,\d+")


class ProtocolError(Exception):
    """A malformed reply, a bad CRC or a Modbus exception."""


class ModbusRejected(ProtocolError):
    """The inverter answered, and refused: the link itself is fine."""


def crc16(data: bytes) -> bytes:
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return bytes((crc & 0xFF, crc >> 8))


def _frame(body: bytes) -> bytes:
    return body + crc16(body)


def read_frame(address: int, count: int, slave: int = SLAVE, max_read: int = MAX_READ) -> bytes:
    if not 1 <= count <= max_read:
        raise ValueError(f"read of {count} registers (1..{max_read})")
    return _frame(bytes((slave, READ)) + address.to_bytes(2, "big") + count.to_bytes(2, "big"))


def write_frame(address: int, value: int, slave: int = SLAVE) -> bytes:
    """Function 0x10 for one register (what the Deye app sends)."""
    if not 0 <= value <= 0xFFFF:
        raise ValueError(f"register value {value} out of 0..65535")
    return _frame(bytes((slave, WRITE)) + address.to_bytes(2, "big") + b"\x00\x01\x02" + value.to_bytes(2, "big"))


def at_command(frame: bytes) -> bytes:
    return b"AT+INVDATA=%d,%s\n" % (len(frame), frame.hex().upper().encode())


def is_handshake(reply: str) -> bool:
    return _HANDSHAKE.fullmatch(reply.strip()) is not None


def _body(reply: str) -> bytes:
    reply = reply.strip()
    if not reply.startswith("+ok="):
        raise ProtocolError(f"no +ok= in {reply!r}")
    try:
        raw = bytes.fromhex(reply[4:])
    except ValueError as exc:
        raise ProtocolError(f"not hex: {reply!r}") from exc
    if len(raw) < 5:
        raise ProtocolError(f"too short: {reply!r}")
    body, crc = raw[:-2], raw[-2:]
    if crc16(body) != crc:
        raise ProtocolError(f"CRC mismatch: {reply!r}")
    if body[1] & 0x80:
        raise ModbusRejected(f"Modbus exception 0x{body[2]:02X} (function 0x{body[1] & 0x7F:02X})")
    return body


def parse_read(reply: str, count: int) -> list[int]:
    body = _body(reply)
    if body[1] != READ or body[2] != 2 * count or len(body) != 3 + 2 * count:
        raise ProtocolError(f"not a read of {count} registers: {reply!r}")
    return [int.from_bytes(body[i:i + 2], "big") for i in range(3, len(body), 2)]


def check_write_ack(reply: str, frame: bytes) -> None:
    body = _body(reply)
    if body[1] != WRITE or body[2:6] != frame[2:6]:
        raise ProtocolError(f"write not acknowledged: {reply!r}")

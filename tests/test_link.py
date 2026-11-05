"""The BLE console link (transport.py) with a fake GATT: the characteristics, the scan and the probes."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.deye_bluetooth import protocol as p
from custom_components.deye_bluetooth.transport import Link, LinkError


async def test_link_splits_reads_to_the_option() -> None:
    link = Link(slave=2, max_read=3, write_char=p.WRITE_CHAR, notify_char=p.NOTIFY_CHAR, reply_timeout=1, connect_attempts=1)
    sent = []

    async def command(payload: bytes) -> str:
        sent.append(payload)
        frame = bytes.fromhex(payload.decode().split(",")[1])
        assert frame[0] == 2  # the slave of the option
        count = frame[5]
        body = bytes((2, 3, 2 * count)) + bytes(2 * count)
        return "+ok=" + p._frame(body).hex()

    link._command = command
    assert await link.read(100, 7) == [0] * 7
    assert len(sent) == 3


class _FakeGatt:
    """A logger whose console is one write and one notify characteristic."""

    def __init__(self, chars: dict[str, list[str]], console: tuple[str, str]) -> None:
        char_objects = [SimpleNamespace(uuid=uuid, properties=props) for uuid, props in chars.items()]
        self.services = [SimpleNamespace(characteristics=char_objects)]
        self.console = console
        self.is_connected = True
        self.written: list[str] = []
        self.notifying: dict[str, object] = {}

    async def start_notify(self, char: str, callback) -> None:
        self.notifying[char] = callback

    async def stop_notify(self, char: str) -> None:
        del self.notifying[char]

    async def write_gatt_char(self, char: str, payload: bytes, response: bool) -> None:
        self.written.append(char)
        if char == self.console[0] and self.console[1] in self.notifying:
            self.notifying[self.console[1]](None, bytearray(b"+ok=21521,21521\n"))

    async def disconnect(self) -> None:
        self.is_connected = False


def _new_link(reply_timeout: float = 0.01) -> Link:
    return Link(slave=1, max_read=16, write_char=p.WRITE_CHAR, notify_char=p.NOTIFY_CHAR,
                reply_timeout=reply_timeout, connect_attempts=1)


async def _connect(gatt: _FakeGatt, link: Link | None = None) -> Link:
    link = link or _new_link()
    with patch("custom_components.deye_bluetooth.transport.establish_connection", new=AsyncMock(return_value=gatt)):
        await link.connect(MagicMock())
    return link


async def test_the_configured_characteristics_are_used_without_a_search() -> None:
    other = "0000aaaa-0000-1000-8000-00805f9b34fb"
    chars = {other: ["write", "notify"], p.WRITE_CHAR: ["write"], p.NOTIFY_CHAR: ["notify"]}
    gatt = _FakeGatt(chars, (p.WRITE_CHAR, p.NOTIFY_CHAR))
    link = await _connect(gatt)
    assert link.connected
    assert gatt.written == [p.WRITE_CHAR]
    assert link.found_console is None  # nothing to remember


async def test_other_characteristics_are_found_by_the_handshake() -> None:
    write, notify, decoy = (f"0000{n}-0000-1000-8000-00805f9b34fb" for n in ("aaa1", "aaa2", "aaa3"))
    gatt = _FakeGatt({decoy: ["write", "notify"], write: ["write"], notify: ["notify"]}, (write, notify))
    link = await _connect(gatt)
    assert link.connected
    assert gatt.written[-1] == write
    assert list(gatt.notifying) == [notify]  # the decoy does not stay subscribed
    assert (link._write_char, link._notify_char) == (write, notify)
    assert link.found_console == (write, notify)  # the coordinator keeps it in the entry


async def test_a_failed_scan_leaves_the_configured_pair_alone() -> None:
    write, notify = (f"0000{n}-0000-1000-8000-00805f9b34fb" for n in ("aaa1", "aaa2"))
    gatt = _FakeGatt({write: ["write"], notify: ["notify"]}, ("none", "none"))
    link = _new_link()
    with pytest.raises(LinkError, match="no console"):
        await _connect(gatt, link)
    assert (link._write_char, link._notify_char) == (p.WRITE_CHAR, p.NOTIFY_CHAR)  # not the last pair tried
    assert link.found_console is None
    assert not gatt.is_connected


async def test_the_scan_probes_briefly_and_the_configured_pair_gets_the_full_reply_timeout() -> None:
    write, notify = (f"0000{n}-0000-1000-8000-00805f9b34fb" for n in ("aaa1", "aaa2"))
    probes: list[tuple[str | None, float | None]] = []

    async def no_reply(payload, *, write_char=None, timeout=None):
        probes.append((write_char, timeout))
        raise LinkError("no reply (test)")

    link = _new_link(reply_timeout=10)
    link._command = no_reply
    with pytest.raises(LinkError, match="no console"):
        await _connect(_FakeGatt({write: ["write"], notify: ["notify"]}, ("none", "none")), link)
    assert probes == [(write, 2.0)]  # a scanned pair: min(reply timeout, 2 s)

    probes.clear()
    link = _new_link(reply_timeout=10)
    link._command = no_reply
    with pytest.raises(LinkError):
        await _connect(_FakeGatt({p.WRITE_CHAR: ["write"], p.NOTIFY_CHAR: ["notify"]}, ("none", "none")), link)
    assert probes == [(p.WRITE_CHAR, 10)]  # the configured pair: the reply timeout as configured


async def test_a_logger_without_a_console_fails_and_lets_go_of_the_link() -> None:
    uuid = "0000aaa1-0000-1000-8000-00805f9b34fb"
    gatt = _FakeGatt({uuid: ["write", "notify"]}, ("none", "none"))
    with pytest.raises(LinkError, match="no console"):
        await _connect(gatt)
    assert not gatt.is_connected


async def test_a_handshake_failure_on_the_configured_pair_does_not_start_a_search() -> None:
    other = "0000aaa1-0000-1000-8000-00805f9b34fb"
    gatt = _FakeGatt({other: ["write", "notify"], p.WRITE_CHAR: ["write"], p.NOTIFY_CHAR: ["notify"]}, (other, other))
    with pytest.raises(LinkError):
        await _connect(gatt)
    assert gatt.written == [p.WRITE_CHAR]

"""The GATT link to the logger: one AT command, one notification back.

The logger takes a single central at a time (close the Deye phone app) and
stops advertising while connected, so the link is held open between polls and
rebuilt only when it drops.
"""
from __future__ import annotations

import asyncio
import logging

from bleak import BleakClient
from bleak.backends.device import BLEDevice
from bleak_retry_connector import establish_connection

from . import protocol as p

_LOGGER = logging.getLogger(__name__)


class LinkError(Exception):
    """No link, no reply, or a reply that makes no sense."""


class RegisterRejected(LinkError):
    """The inverter refused these registers; the link is fine."""


class Link:
    def __init__(
        self, *, slave: int, max_read: int, write_char: str, notify_char: str, reply_timeout: float, connect_attempts: int
    ) -> None:
        self._slave = slave
        self._max_read = max_read
        self._write_char = write_char
        self._notify_char = notify_char
        self._reply_timeout = reply_timeout
        self._connect_attempts = connect_attempts
        self._client: BleakClient | None = None
        self._reply: asyncio.Future[str] | None = None

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def connect(self, device: BLEDevice) -> None:
        await self.close()
        try:
            client = await establish_connection(
                BleakClient, device, device.name or device.address,
                disconnected_callback=self._on_disconnect, max_attempts=self._connect_attempts,
            )
        except Exception as exc:  # noqa: BLE001 — bleak raises many kinds
            raise LinkError(f"connect failed: {exc}") from exc
        self._client = client
        try:
            await client.start_notify(self._notify_char, self._on_notify)
            if not p.is_handshake(await self._command(b"AT+DTYPE\n")):
                raise LinkError("unexpected handshake reply")
        except LinkError:
            await self.close()  # a held link would lock the logger out
            raise
        except Exception as exc:  # noqa: BLE001 — start_notify raises many kinds
            await self.close()
            raise LinkError(f"setup failed: {exc}") from exc

    async def close(self) -> None:
        client, self._client = self._client, None
        if client is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(client.disconnect()), self._reply_timeout)
        except Exception:  # noqa: BLE001 — best effort, never blocks the caller
            _LOGGER.debug("Deye BLE disconnect did not finish cleanly", exc_info=True)

    def _on_disconnect(self, _client: BleakClient) -> None:
        self._client = None
        if self._reply is not None and not self._reply.done():
            self._reply.set_exception(LinkError("link dropped"))

    def _on_notify(self, _char, data: bytearray) -> None:
        if self._reply is not None and not self._reply.done():
            self._reply.set_result(bytes(data).decode("ascii", "replace").strip())

    async def _command(self, payload: bytes) -> str:
        client = self._client
        if client is None:
            raise LinkError("not connected")
        self._reply = asyncio.get_running_loop().create_future()
        try:
            await client.write_gatt_char(self._write_char, payload, response=True)
            return await asyncio.wait_for(self._reply, self._reply_timeout)
        except LinkError:
            raise
        except TimeoutError as exc:
            raise LinkError(f"no reply to {payload[:24]!r}") from exc
        except Exception as exc:  # noqa: BLE001 — a GATT write can raise many kinds
            raise LinkError(f"write failed: {exc}") from exc
        finally:
            self._reply = None

    async def read(self, address: int, count: int) -> list[int]:
        """Any number of registers, in reads the logger takes."""
        words: list[int] = []
        while len(words) < count:
            words += await self._read(address + len(words), min(count - len(words), self._max_read))
        return words

    async def _read(self, address: int, count: int) -> list[int]:
        frame = p.read_frame(address, count, self._slave, self._max_read)
        reply = await self._command(p.at_command(frame))
        try:
            return p.parse_read(reply, count)
        except p.ModbusRejected as exc:
            raise RegisterRejected(str(exc)) from exc
        except p.ProtocolError as exc:
            raise LinkError(str(exc)) from exc

    async def write(self, address: int, value: int) -> None:
        frame = p.write_frame(address, value, self._slave)
        reply = await self._command(p.at_command(frame))
        try:
            p.check_write_ack(reply, frame)
        except p.ProtocolError as exc:
            raise LinkError(str(exc)) from exc

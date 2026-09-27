from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import AsyncIterator
from typing import Any, Protocol, runtime_checkable

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError
from rfnry_senses.models import Identity, IdentityLike, Image, as_identity
from rfnry_senses.transport import Played
from rfnry_senses.wire import PROTOCOL, control, decode, encode_audio, parse_hello, parse_played

logger = logging.getLogger(__name__)


@runtime_checkable
class Connection(Protocol):
    async def receive(self) -> str | bytes: ...
    async def send_text(self, data: str) -> None: ...
    async def send_bytes(self, data: bytes) -> None: ...
    async def close(self) -> None: ...


async def serve(
    connection: Connection, *, identity: IdentityLike, scope: str
) -> WebSocketTransport | None:
    person = as_identity(identity)
    if not scope:
        raise InputError("transport: scope is empty")
    try:
        first = await connection.receive()
        if not isinstance(first, str):
            raise InputError("wire: the first frame must be a hello")
        check_hello(first)
    except InputError as exc:
        await _refuse(connection, str(exc))
        return None
    except Exception:
        return None
    return WebSocketTransport(connection, person, scope)


def check_hello(text: str) -> None:
    hello = parse_hello(text)
    if hello.protocol != PROTOCOL:
        raise InputError(
            f"transport: protocol {hello.protocol} is not supported; this server speaks {PROTOCOL}"
        )


async def _refuse(connection: Connection, message: str) -> None:
    with contextlib.suppress(Exception):
        await connection.send_text(control({"type": "error", "message": message}))
    with contextlib.suppress(Exception):
        await connection.close()


class WebSocketTransport:
    def __init__(self, connection: Connection, identity: Identity, scope: str) -> None:
        self.identity = identity
        self.scope = scope
        self._connection = connection
        self._pending: deque[str | bytes] = deque()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._writer: asyncio.Task[None] | None = None
        self._closed = False

    async def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]:
        while not self._closed:
            try:
                message = await self._connection.receive()
            except Exception:
                return
            try:
                item = decode(message) if isinstance(message, bytes) else parse_played(message)
            except InputError as exc:
                logger.warning("transport: dropped a frame: %s", exc)
                continue
            yield item

    async def send_audio(self, frame: AudioFrame, utterance: int) -> None:
        self._put(encode_audio(frame, utterance))

    async def send_control(self, control_frame: dict[str, Any]) -> None:
        self._put(control(control_frame))

    def clear_audio(self) -> None:
        self._pending = deque(item for item in self._pending if isinstance(item, str))

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._idle.wait(), 1.0)
        if self._writer is not None:
            self._writer.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._writer
        with contextlib.suppress(Exception):
            await self._connection.close()

    def _put(self, item: str | bytes) -> None:
        if self._closed or (self._writer is not None and self._writer.done()):
            return
        self._pending.append(item)
        self._idle.clear()
        self._wake.set()
        if self._writer is None:
            self._writer = asyncio.create_task(self._write())

    async def _write(self) -> None:
        while True:
            while not self._pending:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
            item = self._pending.popleft()
            try:
                if isinstance(item, bytes):
                    await self._connection.send_bytes(item)
                else:
                    await self._connection.send_text(item)
            except Exception:
                self._pending.clear()
                self._idle.set()
                return

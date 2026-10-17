import asyncio
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.models import Identity, IdentityLike, Image, as_identity


class Played(BaseModel):
    utterance: int
    ms: int


@runtime_checkable
class Transport(Protocol):
    @property
    def identity(self) -> IdentityLike: ...
    @property
    def scope(self) -> str: ...

    def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]: ...
    async def send_audio(self, frame: AudioFrame, utterance: int) -> None: ...
    async def send_control(self, control: dict[str, Any]) -> None: ...
    def clear_audio(self) -> None: ...
    async def close(self) -> None: ...


class Loopback:
    def __init__(
        self,
        identity: IdentityLike,
        scope: str,
        *,
        played: Callable[[int, float], int] | None = None,
        answer: bool = True,
    ) -> None:
        self.identity: Identity = as_identity(identity)
        self.scope = scope
        self.audio: list[AudioFrame] = []
        self.controls: list[dict[str, Any]] = []
        self.closed = False
        self._played = played
        self._answer = answer
        self._sent: dict[int, float] = {}
        self._inbox: asyncio.Queue[AudioFrame | Image | Played | None] = asyncio.Queue()

    async def speak(self, frame: AudioFrame) -> None:
        await self._inbox.put(frame)

    async def show(self, jpeg: bytes) -> None:
        await self._inbox.put(Image(media_type="image/jpeg", data=jpeg))

    async def hang_up(self) -> None:
        await self._inbox.put(None)

    async def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]:
        while (item := await self._inbox.get()) is not None:
            yield item

    async def send_audio(self, frame: AudioFrame, utterance: int) -> None:
        self.audio.append(frame)
        self._sent[utterance] = self._sent.get(utterance, 0.0) + frame.duration_ms

    async def send_control(self, control: dict[str, Any]) -> None:
        self.controls.append(control)
        if self._answer and control["type"] in ("stop", "silent"):
            utterance = control["utterance"]
            sent = self._sent.get(utterance, 0.0)
            ms = self._played(utterance, sent) if self._played is not None else int(sent)
            self._inbox.put_nowait(Played(utterance=utterance, ms=ms))

    def clear_audio(self) -> None:
        pass

    async def close(self) -> None:
        self.closed = True
        self._inbox.put_nowait(None)

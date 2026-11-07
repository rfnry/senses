import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from rfnry_senses.contracts import (
    AudioFrame,
    LiveAudio,
    LiveDone,
    LiveEvent,
    LiveSaid,
    LiveStarted,
    Transcript,
    UtteranceEnd,
)
from rfnry_senses.models import Content, Identity, IdentityLike, Image, ToolSpec
from rfnry_senses.transport import Loopback, Played

ANA = Identity(role="user", id="ana", name="Ana")
HOST = Identity(role="assistant", id="host", name="Host")
JPEG = b"\xff\xd8\xff\xe0frame\xff\xd9"


@dataclass(frozen=True)
class Lookalike:
    role: Literal["user", "assistant", "system"]
    id: str
    name: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


def lookalike(identity: Identity) -> Lookalike:
    return Lookalike(identity.role, identity.id, identity.name, dict(identity.metadata))


def pcm(ms: int, rate: int = 16000) -> AudioFrame:
    return AudioFrame(data=bytes(rate * ms // 1000 * 2), sample_rate=rate)


def response(*words: str, ms: int = 100) -> list[LiveEvent]:
    events: list[LiveEvent] = [LiveStarted()]
    for word in words:
        events += [LiveSaid(text=word), LiveAudio(frame=pcm(ms, 24000))]
    return [*events, LiveDone()]


async def until(condition: Callable[[], bool], timeout: float = 1.0) -> None:
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.001)


async def next_of[T](events: AsyncIterator[Any], kind: type[T], timeout: float = 1.0) -> T:
    async with asyncio.timeout(timeout):
        async for event in events:
            if isinstance(event, kind):
                return event
    raise AssertionError(f"no {kind.__name__} before the stream ended")


class ScriptedConnection:
    duplex = False

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.audio: list[AudioFrame] = []
        self.images: list[Image] = []
        self.results: list[tuple[str, Content, bool, bool]] = []
        self.interrupts: list[tuple[int, str, str | None]] = []
        self.refuse_interrupt = False
        self.tagging = False
        self.closed = False
        self._inbox: asyncio.Queue[LiveEvent | None] = asyncio.Queue()

    def emit(self, *events: LiveEvent) -> None:
        for event in events:
            self._inbox.put_nowait(event)

    def emit_end(self) -> None:
        self._inbox.put_nowait(None)

    async def send_audio(self, frame: AudioFrame) -> None:
        self.audio.append(frame)

    async def send_image(self, image: Image) -> None:
        self.images.append(image)

    async def send_text(self, text: str, *, respond: bool) -> str | None:
        await asyncio.sleep(0)
        self.calls.append(("text", text, respond))
        return self._tag() if respond else None

    async def say(self, text: str) -> str | None:
        await asyncio.sleep(0)
        self.calls.append(("say", text))
        return self._tag()

    def _tag(self) -> str | None:
        return f"request-{len(self.calls)}" if self.tagging else None

    async def interrupt(self, played_ms: int, *, heard: str, response: str | None) -> None:
        if self.refuse_interrupt:
            raise RuntimeError("cannot cut a response")
        self.interrupts.append((played_ms, heard, response))

    async def submit_result(
        self, call_id: str, result: Content, is_error: bool, *, respond: bool
    ) -> None:
        self.results.append((call_id, result, is_error, respond))

    async def events(self) -> AsyncIterator[LiveEvent]:
        while (event := await self._inbox.get()) is not None:
            yield event

    async def close(self) -> None:
        self.closed = True
        self._inbox.put_nowait(None)


class ScriptedLive:
    def __init__(self) -> None:
        self.connection = ScriptedConnection()
        self.opened: dict[str, Any] = {}

    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> ScriptedConnection:
        self.opened = {"tools": tools, "instructions": instructions}
        return self.connection


class DuplexConnection(ScriptedConnection):
    duplex = True

    async def say(self, text: str) -> str | None:
        await super().say(text)
        return f"request-{len(self.calls)}"


class DuplexLive(ScriptedLive):
    def __init__(self) -> None:
        super().__init__()
        self.connection = DuplexConnection()


class ScriptedTranscription:
    def __init__(self) -> None:
        self.fed: list[AudioFrame] = []
        self._inbox: asyncio.Queue[Transcript | UtteranceEnd | None] = asyncio.Queue()

    def stream(self) -> ScriptedTranscription:
        return self

    def hear(self, *texts: str, final: bool = True, end: bool = True) -> None:
        for text in texts:
            self._inbox.put_nowait(Transcript(text=text, final=final))
        if end:
            self._inbox.put_nowait(UtteranceEnd())

    async def feed(self, frame: AudioFrame) -> None:
        self.fed.append(frame)

    async def events(self) -> AsyncIterator[Transcript | UtteranceEnd]:
        while (event := await self._inbox.get()) is not None:
            yield event

    async def close(self) -> None:
        self._inbox.put_nowait(None)


class Tone:
    def __init__(self, ms_per_word: int = 100) -> None:
        self.ms = ms_per_word
        self.spoken: list[str] = []
        self.held: str | None = None
        self.release = asyncio.Event()
        self.fail = False

    async def speak(self, text: str) -> AsyncIterator[AudioFrame]:
        self.spoken.append(text)
        if self.fail:
            raise RuntimeError("synthesis failed")
        if text == self.held:
            await self.release.wait()
        for _ in text.split():
            yield pcm(self.ms)


class SlowLoopback(Loopback):
    def __init__(self, identity: IdentityLike, scope: str, *, delay: float | None) -> None:
        super().__init__(identity, scope, answer=False)
        self.delay = delay
        self._reported: set[int] = set()

    async def send_control(self, control: dict[str, Any]) -> None:
        await super().send_control(control)
        if self.delay is None or control["type"] not in ("stop", "silent"):
            return
        last = control["utterance"]
        through = {n for n in self._sent if n <= last} if control["type"] == "stop" else set()
        for utterance in sorted((through | {last}) - self._reported):
            self._reported.add(utterance)
            played = Played(utterance=utterance, ms=int(self._sent.get(utterance, 0.0)))
            asyncio.get_running_loop().call_later(self.delay, self._inbox.put_nowait, played)


class AwaitingLoopback(Loopback):
    async def send_control(self, control: dict[str, Any]) -> None:
        await asyncio.sleep(0)
        await super().send_control(control)

import asyncio
import importlib
import os
import sys
from collections.abc import AsyncIterator
from typing import Any

from websockets.asyncio.server import ServerConnection, serve as listen

from rfnry_senses import (
    AudioFrame,
    Closed,
    Identity,
    Session,
    SpeechModel,
    Transcript,
    TranscriptionModel,
    TurnEnded,
    UtteranceEnd,
    serve,
)

ANA = Identity(role="user", id="ana", name="Ana")


class HearsHello:
    def __init__(self) -> None:
        self.fed = 0
        self.inbox: asyncio.Queue[Transcript | UtteranceEnd | None] = asyncio.Queue()

    def stream(self) -> "HearsHello":
        return self

    async def feed(self, frame: AudioFrame) -> None:
        self.fed += 1
        if self.fed == 10:
            self.inbox.put_nowait(Transcript(text="hello", final=True))
            self.inbox.put_nowait(UtteranceEnd())

    async def events(self) -> AsyncIterator[Transcript | UtteranceEnd]:
        while (event := await self.inbox.get()) is not None:
            yield event

    async def close(self) -> None:
        self.inbox.put_nowait(None)


class Tone:
    async def speak(self, text: str) -> AsyncIterator[AudioFrame]:
        for _ in text.split():
            yield AudioFrame(data=bytes(3200), sample_rate=16000)


def models() -> tuple[TranscriptionModel, SpeechModel]:
    name = os.environ.get("RFNRY_LIVE_ADAPTERS")
    if not name:
        return HearsHello(), Tone()
    adapters: Any = importlib.import_module(name)
    return adapters.transcription(), adapters.speech()


class Adapted:
    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws

    async def receive(self) -> str | bytes:
        return await self.ws.recv()

    async def send_text(self, data: str) -> None:
        await self.ws.send(data)

    async def send_bytes(self, data: bytes) -> None:
        await self.ws.send(data)

    async def close(self) -> None:
        await self.ws.close()


async def handler(ws: ServerConnection) -> None:
    link = await serve(Adapted(ws), identity=ANA, scope="acme")
    if link is None:
        return
    transcription, speech = models()
    async with Session(link, transcription=transcription, speech=speech) as session:
        async for event in session.events():
            if isinstance(event, TurnEnded):
                asyncio.create_task(session.say("Hi there.", triggered_by=event.speaker))
            if isinstance(event, Closed):
                print(f"closed {event.reason}", flush=True)


async def main(port: int) -> None:
    async with listen(handler, "127.0.0.1", port) as server:
        print(f"listening {next(iter(server.sockets)).getsockname()[1]}", flush=True)
        await asyncio.Future()


if __name__ == "__main__":
    asyncio.run(main(int(sys.argv[1])))

import asyncio
from collections.abc import AsyncIterator

from rfnry_senses import AudioFrame, Identity, Loopback, Session, Transcript, UtteranceEnd

ANA = Identity(role="user", id="ana", name="Ana")
HOST = Identity(role="assistant", id="host", name="Host")
REPLY = "Your table is ready. It is by the window. Shall I order the usual?"


class Ears:
    def __init__(self) -> None:
        self.heard: asyncio.Queue[Transcript | UtteranceEnd] = asyncio.Queue()

    def stream(self) -> "Ears":
        return self

    async def feed(self, frame: AudioFrame) -> None:
        pass

    async def events(self) -> AsyncIterator[Transcript | UtteranceEnd]:
        while True:
            yield await self.heard.get()

    async def close(self) -> None:
        pass


class Voice:
    async def speak(self, text: str) -> AsyncIterator[AudioFrame]:
        for _ in text.split():
            await asyncio.sleep(0.1)
            yield AudioFrame(data=bytes(3200), sample_rate=16000)


async def cut_after(seconds: float, link: Loopback) -> None:
    ears = Ears()
    async with Session(link, transcription=ears, speech=Voice()) as session:
        saying = asyncio.create_task(session.say(REPLY, triggered_by=HOST))
        await asyncio.sleep(seconds)
        ears.heard.put_nowait(Transcript(text="Sorry, wait", final=False))
        said = await saying
    print(f"  heard {said.heard!r}, interrupted={said.interrupted}")


async def main() -> None:
    print(f"the assistant says: {REPLY!r}, the guest talks over it at 1.2 s")
    for played in (400, 1200):
        print(f"the client played {played} ms before it stopped")
        await cut_after(1.2, Loopback(ANA, "acme", played=lambda utterance, sent, ms=played: ms))
    print("the client never reports; senses estimates from its clock")
    await cut_after(1.2, Loopback(ANA, "acme", answer=False))


asyncio.run(main())

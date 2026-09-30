import asyncio
from collections.abc import AsyncIterator
from datetime import date

from rfnry_engine import Engine, Event, Message, TextDelta, Tool
from rfnry_senses import Session, Transport, TransportError, TurnEnded

from providers import AnthropicProvider, speech, transcription
from tools import lookup_order

engine = Engine(
    "agents", generation=AnthropicProvider(), tools=[Tool("lookup_order", body=lookup_order)]
)


async def converse(link: Transport) -> None:
    history: list[Message] = []
    async with Session(link, transcription=transcription(), speech=speech()) as session:
        async for event in session.events():
            if not isinstance(event, TurnEnded):
                continue
            words = reply(event.text, session.scope, history)
            try:
                said = await session.say(words, triggered_by=event.speaker)
            except TransportError:
                return
            history.append(Message(role="user", content=event.text))
            if said.heard:
                history.append(Message(role="assistant", content=said.heard))


async def reply(question: str, scope: str, history: list[Message]) -> AsyncIterator[str]:
    deltas: asyncio.Queue[str | None] = asyncio.Queue()

    async def on_event(event: Event) -> None:
        if isinstance(event, TextDelta):
            deltas.put_nowait(event.text)

    async def answer() -> None:
        try:
            await engine.run(
                "support",
                scope=scope,
                input=question,
                history=history,
                context=f"Today is {date.today().isoformat()}.",
                on_event=on_event,
            )
        finally:
            deltas.put_nowait(None)

    work = asyncio.create_task(answer())
    try:
        while (text := await deltas.get()) is not None:
            yield text
        await work
    finally:
        work.cancel()

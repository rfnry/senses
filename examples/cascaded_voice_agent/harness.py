import asyncio
from collections.abc import AsyncIterator

from rfnry_engine import Engine, TextDelta
from rfnry_senses import Closed, Heard, Session, Transport, TransportError, TurnEnded

from providers import AnthropicProvider, GoogleSpeech, OpenAIProvider

engine = Engine("agents", generation=AnthropicProvider())
voice = GoogleSpeech()


async def reply(text: str, scope: str, history: list[dict[str, str]]) -> AsyncIterator[str]:
    chunks: asyncio.Queue[str | None] = asyncio.Queue()

    async def forward(event: object) -> None:
        if isinstance(event, TextDelta):
            chunks.put_nowait(event.text)

    async def run() -> None:
        try:
            await engine.run(
                "concierge", scope=scope, input=text, history=history, on_event=forward
            )
        finally:
            chunks.put_nowait(None)

    running = asyncio.create_task(run())
    try:
        while (chunk := await chunks.get()) is not None:
            yield chunk
        await running
    finally:
        running.cancel()


async def converse(link: Transport, replies: int | None = None) -> None:
    history: list[dict[str, str]] = []
    async with Session(link, transcription=OpenAIProvider(), speech=voice) as session:
        async for event in session.events():
            match event:
                case Heard(final=True, text=text, speaker=speaker):
                    print(f"heard  {speaker.name}: {text}")
                case TurnEnded(text=text):
                    try:
                        said = await session.say(
                            reply(text, session.scope, history), triggered_by=event.speaker
                        )
                    except TransportError:
                        return
                    print(f"said   {said.heard!r} interrupted={said.interrupted}")
                    history += [
                        {"role": "user", "content": text},
                        {"role": "assistant", "content": said.heard},
                    ]
                    replies = None if replies is None else replies - 1
                    if replies == 0:
                        return
                case Closed(reason=reason, message=message):
                    print(f"closed {reason}: {message}")

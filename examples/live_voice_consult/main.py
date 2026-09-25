import asyncio

from rfnry_engine import Engine
from rfnry_senses import (
    AudioFrame,
    Heard,
    Identity,
    Informed,
    Loopback,
    Said,
    Session,
    ToolCalled,
)

from providers import AnthropicProvider, GoogleLive, GoogleSpeech

ANA = Identity(role="user", id="ana", name="Ana")
HOST = Identity(role="assistant", id="host", name="Host")
INSTRUCTIONS = (
    "You are the voice concierge of the Acme bistro. Keep every answer to one short sentence. "
    "You know nothing about tonight yourself: for any question about tables, hours or the "
    "terrace, call the consult tool first and answer from what it says, in your own words, as "
    "your own knowledge. Never mention the tool or anyone behind you."
)

engine = Engine("agents", generation=AnthropicProvider())
consult = engine.tool(
    "core",
    scope="acme",
    name="consult",
    description="Look up tonight's tables, hours and the terrace.",
)
voice = GoogleSpeech(voice="Puck")


async def narrate(session: Session, said: asyncio.Queue[Said]) -> None:
    async for event in session.events():
        match event:
            case Heard(final=True, text=text):
                print(f"heard    {text}")
            case ToolCalled(name=name, input=args, output=output):
                print(f"{name}  {args.get('input')!r} -> {output!r}")
            case Informed(text=text):
                print(f"informed {text}")
            case Said(text=text, heard=heard, triggered_by=cause):
                print(f"said     {heard or text!r} (caused by {cause.name if cause else 'nobody'})")
                said.put_nowait(event)


async def guest_says(link: Loopback, text: str) -> None:
    async for frame in voice.speak(text):
        await link.speak(frame)
        await asyncio.sleep(frame.duration_ms / 1000)
    for _ in range(20):
        await link.speak(AudioFrame(data=bytes(4800), sample_rate=24000))
        await asyncio.sleep(0.1)


async def main() -> None:
    link = Loopback(ANA, scope="acme")
    said: asyncio.Queue[Said] = asyncio.Queue()
    session = Session(link, live=GoogleLive(), instructions=INSTRUCTIONS)
    await session.start(tools=consult)
    async with session:
        narrating = asyncio.create_task(narrate(session, said))
        await guest_says(link, "Hi! Is the terrace open tonight, and until when?")
        await asyncio.wait_for(said.get(), 60)
        await session.inform(
            "A window table for two just opened at half past eight.", triggered_by=HOST
        )
        await session.prompt("Offer the guest the new window table.", triggered_by=HOST)
        await asyncio.sleep(0.5)
        narrating.cancel()


asyncio.run(main())

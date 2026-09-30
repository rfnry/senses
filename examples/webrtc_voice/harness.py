import asyncio

from rfnry_engine import Engine
from rfnry_senses import Closed, Heard, Said, Session, ToolCalled, Transport

from providers import AnthropicProvider, GoogleLive

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
answered = asyncio.Event()


async def converse(link: Transport) -> None:
    session = Session(link, live=GoogleLive(), instructions=INSTRUCTIONS)
    await session.start(tools=consult)
    async with session:
        async for event in session.events():
            match event:
                case Heard(final=True, text=text):
                    print(f"heard    {text}")
                case ToolCalled(input=args, output=output):
                    print(f"consult  {args.get('input')!r} -> {output!r}")
                case Said(heard=heard, interrupted=interrupted):
                    print(f"said     {heard!r} interrupted={interrupted}")
                    answered.set()
                case Closed(reason=reason, message=message):
                    print(f"closed   {reason}: {message}")

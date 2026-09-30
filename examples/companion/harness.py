import asyncio

from rfnry_engine import Engine
from rfnry_senses import (
    Closed,
    Heard,
    Identity,
    Informed,
    Looked,
    Said,
    SensesTools,
    Session,
    ToolCalled,
    Transport,
)

from providers import AnthropicProvider, GoogleLive

MIND = Identity(role="assistant", id="mind", name="Mind")
INSTRUCTIONS = (
    "You talk with a person who hears you, and you hear them through their microphone and see "
    "through their camera or, when they share it, their screen. Keep every answer to one or two "
    "short sentences. A second mind works behind you: for anything about what you see, or that "
    "needs thought, call the consult tool with the question and answer from what it says."
)

engine = Engine("agents", generation=AnthropicProvider())


async def companion(link: Transport) -> Session:
    session = Session(link, live=GoogleLive(), video=True, instructions=INSTRUCTIONS)
    consult = engine.tool(
        "mind",
        scope=link.scope,
        name="consult",
        description="Ask the mind behind you. It sees what you see: the camera or the screen.",
        tools=SensesTools(session, triggered_by=MIND).only("look", "inform"),
    )
    await session.start(tools=consult)
    return session


async def narrate(session: Session, answered: asyncio.Event) -> None:
    async for event in session.events():
        match event:
            case Heard(final=True, text=text):
                print(f"heard    {text}")
            case Looked(frames=frames, triggered_by=cause):
                print(f"looked   {frames} frame(s), by {cause.name}")
            case ToolCalled(name=name, input=args, output=output):
                print(f"{name}  {args.get('input')!r} -> {output!r}")
            case Informed(text=text, triggered_by=cause):
                print(f"informed {text!r}, by {cause.name}")
            case Said(text=text, heard=heard, triggered_by=cause):
                print(f"said     {heard or text!r} (caused by {cause.name if cause else 'nobody'})")
                if heard:
                    answered.set()
            case Closed(reason=reason, message=message):
                print(f"closed   {reason}: {message}")

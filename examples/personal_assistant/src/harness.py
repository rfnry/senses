from datetime import date
from pathlib import Path

from rfnry_engine import Engine
from rfnry_senses import Identity, SensesTools, Session, ToolCalled, Transport

from providers import AnthropicProvider, GoogleLive

ROOT = Path(__file__).resolve().parent.parent
ASSISTANT = Identity(role="assistant", id="assistant", name="Assistant")
INSTRUCTIONS = (
    "You are Jordan's personal assistant. You hear Jordan through the microphone and see through "
    "the camera. Keep every answer to one or two short sentences. A second mind works behind you "
    "and sees what you see: for anything about the home, money or paperwork, or anything you "
    "would need to read closely, call the consult tool with the question and answer from what it "
    "says, in your own words."
)

engine = Engine(ROOT / "agents", generation=AnthropicProvider())


async def converse(link: Transport) -> None:
    session = Session(link, live=GoogleLive(), video=True, instructions=INSTRUCTIONS)
    consult = engine.tool(
        "assistant",
        scope=link.scope,
        name="consult",
        description="Ask the mind behind you. It sees the camera and knows the home's paperwork.",
        context=f"Today is {date.today().isoformat()}.",
        tools=SensesTools(session, triggered_by=ASSISTANT).only("look", "inform"),
    )
    await session.start(tools=consult)
    async with session:
        async for event in session.events():
            if isinstance(event, ToolCalled):
                print(f"consult {event.input['input']!r} -> {event.output}")

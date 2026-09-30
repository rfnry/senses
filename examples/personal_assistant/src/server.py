from rfnry_senses import Connection, Identity, SensesTools, Session, ToolCalled, serve

import harness
from providers import GoogleLive

ASSISTANT = Identity(role="assistant", id="assistant", name="Assistant")
INSTRUCTIONS = (
    "You are Jordan's personal assistant. You hear Jordan through the microphone and see through "
    "the camera. Keep every answer to one or two short sentences. A second mind works behind you "
    "and sees what you see: for anything about the home, money or paperwork, or anything you "
    "would need to read closely, call the consult tool with the question and answer from what it "
    "says, in your own words."
)


async def converse(connection: Connection, *, identity: Identity, scope: str) -> None:
    link = await serve(connection, identity=identity, scope=scope)
    if link is None:
        return
    session = Session(link, live=GoogleLive(), video=True, instructions=INSTRUCTIONS)
    consult = harness.consult(link.scope, SensesTools(session, triggered_by=ASSISTANT))
    await session.start(tools=consult)
    async with session:
        async for event in session.events():
            if isinstance(event, ToolCalled):
                print(f"consult {event.input['input']!r} -> {event.output}")

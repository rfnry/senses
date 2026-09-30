from rfnry_engine import Engine
from rfnry_senses import Session, ToolCalled, Transport

from providers import GoogleLive, decision

INSTRUCTIONS = (
    "You assist the front desk and the nurses of Acme Clinic, never patients directly. When a "
    "staff member tells you about a message or a call from a patient, call consult with the "
    "patient's message in their own words. Then tell the staff member, in one or two sentences, "
    "how urgent it is and who takes it: the front desk for scheduling, or the nurse line. When "
    "the department's confidence is below 0.6, say triage is not sure and a nurse will call the "
    "patient back."
)

engine = Engine("agents", decision=decision())


async def converse(link: Transport) -> None:
    consult = engine.tool(
        "triage",
        scope=link.scope,
        name="consult",
        description="Triage a patient's message: how urgent it is, and who at the clinic takes it.",
    )
    session = Session(link, live=GoogleLive(), instructions=INSTRUCTIONS)
    await session.start(tools=consult)
    async with session:
        async for event in session.events():
            if isinstance(event, ToolCalled):
                print(f"consult {event.input['input']!r} -> {event.output}")

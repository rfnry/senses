import asyncio

from rfnry_engine import Engine, Tools
from rfnry_senses import Closed, Heard, Identity, Said, SensesTools, Session
from rfnry_thread import Client, ThreadTools

from providers import AnthropicProvider

CONCIERGE = Identity(role="assistant", id="concierge", name="Concierge")
HOST = Identity(role="assistant", id="host", name="Host")
INSTRUCTIONS = (
    "You are the voice concierge of the Acme bistro. Keep every answer to one short sentence. "
    "For any question about tables, hours or the terrace, call the consult tool first."
)

engine = Engine("agents", generation=AnthropicProvider())


async def mirror(session: Session, post, said: asyncio.Queue[Said]) -> None:
    async for event in session.events():
        match event:
            case Heard(final=True):
                await post(event.speaker, event.text)
            case Said(heard=heard, triggered_by=cause) if heard:
                print(f"said  {heard!r} (caused by {cause.name if cause else 'nobody'})")
                await post(CONCIERGE, heard)
                said.put_nowait(event)
            case Closed(reason=reason, message=message):
                print(f"closed {reason}: {message}")


def voice_tools(concierge: Client, thread_id: str) -> Tools:
    consult = engine.tool(
        "core", scope="acme", name="consult", description="Ask the reasoning mind."
    )
    transcript = ThreadTools(concierge, thread_id=thread_id).only("read_recent")
    return consult + transcript


async def tell_host(session: Session, text: str) -> str:
    behind = SensesTools(session, triggered_by=HOST)
    run = await engine.run("host", scope=session.scope, input=text, tools=behind)
    return run.reply

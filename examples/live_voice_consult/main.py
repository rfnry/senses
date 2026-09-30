import asyncio

from rfnry_senses import Closed, Heard, Identity, Informed, Loopback, Said, Session, ToolCalled

from client import guest_says
from harness import INSTRUCTIONS, consult
from providers import GoogleLive

ANA = Identity(role="user", id="ana", name="Ana")
HOST = Identity(role="assistant", id="host", name="Host")


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
            case Closed(reason=reason, message=message):
                print(f"closed   {reason}: {message}")


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

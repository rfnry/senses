import asyncio

from rfnry_senses import Identity, Loopback, Said, Session
from rfnry_thread import InMemoryStorage, Message, Server, resolve

from client import guest_says
from harness import CONCIERGE, INSTRUCTIONS, mirror, tell_host, voice_tools
from providers import GoogleLive

ANA = Identity(role="user", id="ana", name="Ana")


async def main() -> None:
    server = Server(InMemoryStorage())
    guest = await server.connect(identity=ANA, scope="acme")
    concierge = await server.connect(identity=CONCIERGE, scope="acme")
    thread = await guest.create_thread(thread_id="table-for-two")
    await guest.add_member(thread.id, concierge.identity)
    clients = {ANA.id: guest, CONCIERGE.id: concierge}

    async def post(who: Identity, text: str) -> None:
        await clients[who.id].send(thread.id, text)

    tools = voice_tools(concierge, thread.id)
    link = Loopback(ANA, scope="acme")
    said: asyncio.Queue[Said] = asyncio.Queue()
    session = Session(link, live=GoogleLive(), instructions=INSTRUCTIONS)
    await session.start(tools=tools)
    async with session:
        mirroring = asyncio.create_task(mirror(session, post, said))
        await guest_says(link, "Hi, is the terrace still open tonight?")
        await asyncio.wait_for(said.get(), 60)
        reply = await tell_host(
            session, "The guest's table for two at eight is ready. Let them know."
        )
        print(f"host: {reply}")
        await asyncio.wait_for(said.get(), 60)
        await asyncio.sleep(0.5)
        mirroring.cancel()

    print("\nthe thread:")
    for event in resolve(await guest.history(thread.id)):
        if isinstance(event, Message):
            print(f"  {event.author.name}: {event.content[0].text}")
    await guest.close()
    await concierge.close()


asyncio.run(main())

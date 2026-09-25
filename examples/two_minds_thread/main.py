import asyncio

from rfnry_engine import Engine
from rfnry_senses import AudioFrame, Heard, Identity, Loopback, Said, SensesTools, Session
from rfnry_thread import InMemoryStorage, Message, Server, ThreadTools, resolve

from providers import AnthropicProvider, GoogleLive, GoogleSpeech

ANA = Identity(role="user", id="ana", name="Ana")
CONCIERGE = Identity(role="assistant", id="concierge", name="Concierge")
HOST = Identity(role="assistant", id="host", name="Host")
INSTRUCTIONS = (
    "You are the voice concierge of the Acme bistro. Keep every answer to one short sentence. "
    "For any question about tables, hours or the terrace, call the consult tool first."
)

engine = Engine("agents", generation=AnthropicProvider())
voice = GoogleSpeech(voice="Puck")


async def mirror(session: Session, post, said: asyncio.Queue[Said]) -> None:
    async for event in session.events():
        match event:
            case Heard(final=True):
                await post(event.speaker, event.text)
            case Said(heard=heard, triggered_by=cause) if heard:
                print(f"said  {heard!r} (caused by {cause.name if cause else 'nobody'})")
                await post(CONCIERGE, heard)
                said.put_nowait(event)


async def guest_says(link: Loopback, text: str) -> None:
    async for frame in voice.speak(text):
        await link.speak(frame)
        await asyncio.sleep(frame.duration_ms / 1000)
    for _ in range(20):
        await link.speak(AudioFrame(data=bytes(4800), sample_rate=24000))
        await asyncio.sleep(0.1)


async def main() -> None:
    server = Server(InMemoryStorage())
    guest = await server.connect(identity=ANA, scope="acme")
    concierge = await server.connect(identity=CONCIERGE, scope="acme")
    thread = await guest.create_thread(thread_id="table-for-two")
    await guest.add_member(thread.id, concierge.identity)
    clients = {ANA.id: guest, CONCIERGE.id: concierge}

    async def post(who: Identity, text: str) -> None:
        await clients[who.id].send(thread.id, text)

    consult = engine.tool(
        "core", scope="acme", name="consult", description="Ask the reasoning mind."
    )
    transcript = ThreadTools(concierge, thread_id=thread.id).only("read_recent")
    link = Loopback(ANA, scope="acme")
    said: asyncio.Queue[Said] = asyncio.Queue()
    session = Session(link, live=GoogleLive(), instructions=INSTRUCTIONS)
    await session.start(tools=consult + transcript)
    async with session:
        mirroring = asyncio.create_task(mirror(session, post, said))
        await guest_says(link, "Hi, is the terrace still open tonight?")
        await asyncio.wait_for(said.get(), 60)
        behind = SensesTools(session, triggered_by=HOST)
        run = await engine.run(
            "host",
            scope=session.scope,
            input="The guest's table for two at eight is ready. Let them know.",
            tools=behind,
        )
        print(f"host: {run.reply}")
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

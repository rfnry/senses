from rfnry_engine import Message
from rfnry_senses import Connection, Identity, Session, TransportError, TurnEnded, serve

import harness
from providers import speech, transcription


async def converse(connection: Connection, *, identity: Identity, scope: str) -> None:
    link = await serve(connection, identity=identity, scope=scope)
    if link is None:
        return
    history: list[Message] = []
    async with Session(link, transcription=transcription(), speech=speech()) as session:
        async for event in session.events():
            if not isinstance(event, TurnEnded):
                continue
            words = harness.run_stream(event.text, session.scope, history)
            try:
                said = await session.say(words, triggered_by=event.speaker)
            except TransportError:
                return
            history.append(Message(role="user", content=event.text))
            if said.heard:
                history.append(Message(role="assistant", content=said.heard))

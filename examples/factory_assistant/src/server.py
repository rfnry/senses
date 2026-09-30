from rfnry_engine import Message
from rfnry_senses import (
    Connection,
    Identity,
    SensesTools,
    Session,
    TransportError,
    TurnEnded,
    serve,
)

import harness
from providers import GoogleSpeech, OpenAIProvider

MAINTENANCE = Identity(role="assistant", id="maintenance", name="Maintenance")


async def converse(connection: Connection, *, identity: Identity, scope: str) -> None:
    link = await serve(connection, identity=identity, scope=scope)
    if link is None:
        return
    history: list[Message] = []
    async with Session(
        link, transcription=OpenAIProvider(), speech=GoogleSpeech(), video=True
    ) as session:
        senses = SensesTools(session, triggered_by=MAINTENANCE)
        async for event in session.events():
            if not isinstance(event, TurnEnded):
                continue
            reply = await harness.run(event.text, session.scope, history, senses)
            try:
                said = await session.say(reply, triggered_by=event.speaker)
            except TransportError:
                return
            history.append(Message(role="user", content=event.text))
            if said.heard:
                history.append(Message(role="assistant", content=said.heard))

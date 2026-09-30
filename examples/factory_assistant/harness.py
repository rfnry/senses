import json
from pathlib import Path

from rfnry_engine import Engine, Message
from rfnry_senses import Identity, SensesTools, Session, Transport, TransportError, TurnEnded

from providers import AnthropicProvider, GoogleSpeech, OpenAIProvider

PLANTS = json.loads(Path("data/plants.json").read_text())["plants"]
MAINTENANCE = Identity(role="assistant", id="maintenance", name="Maintenance")

engine = Engine("agents", generation=AnthropicProvider())


async def converse(link: Transport) -> None:
    facts = json.dumps(PLANTS[link.scope], indent=2, ensure_ascii=False)
    history: list[Message] = []
    async with Session(
        link, transcription=OpenAIProvider(), speech=GoogleSpeech(), video=True
    ) as session:
        eyes = SensesTools(session, triggered_by=MAINTENANCE).only("look")
        async for event in session.events():
            if not isinstance(event, TurnEnded):
                continue
            run = await engine.run(
                "maintenance",
                scope=session.scope,
                input=event.text,
                history=history,
                context=facts,
                tools=eyes,
            )
            try:
                said = await session.say(run.reply, triggered_by=event.speaker)
            except TransportError:
                return
            history.append(Message(role="user", content=event.text))
            if said.heard:
                history.append(Message(role="assistant", content=said.heard))

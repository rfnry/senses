from pathlib import Path

from rfnry_engine import Engine
from rfnry_senses import Identity, SensesTools, Session, Transport

from providers import AnthropicProvider

ROOT = Path(__file__).resolve().parent.parent
REVIEWER = Identity(role="assistant", id="reviewer", name="Reviewer")

engine = Engine(ROOT / "agents", generation=AnthropicProvider())
screens: list[Session] = []


async def share(link: Transport) -> None:
    async with Session(link, video=True) as screen:
        screens.append(screen)
        try:
            async for _ in screen.events():
                pass
        finally:
            screens.remove(screen)


async def ask(question: str) -> dict:
    screen = screens[-1]
    eyes = SensesTools(screen, triggered_by=REVIEWER).only("look")
    run = await engine.run("reviewer", scope=screen.scope, input=question, tools=eyes)
    return {"reply": run.reply, "run_id": run.run_id}

from rfnry_engine import Engine
from rfnry_senses import Identity, SensesTools, Session, Transport

from providers import decision, generation

REVIEWER = Identity(role="assistant", id="reviewer", name="Reviewer")

engine = Engine("agents", generation=generation(), decision=decision())
screens: dict[str, Session] = {}


async def share(link: Transport) -> None:
    async with Session(link, video=True) as screen:
        screens[link.scope] = screen
        async for _ in screen.events():
            pass
    if screens.get(link.scope) is screen:
        del screens[link.scope]


async def ask(firm: str, question: str) -> dict:
    eyes = SensesTools(screens[firm], triggered_by=REVIEWER).only("look")
    run = await engine.run("reviewer", scope=firm, input=question, tools=eyes)
    return {"reply": run.reply, "run_id": run.run_id}

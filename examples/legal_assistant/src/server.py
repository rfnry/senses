from rfnry_senses import Connection, Identity, SensesTools, Session, serve

import harness

REVIEWER = Identity(role="assistant", id="reviewer", name="Reviewer")

screens: list[Session] = []


async def share(connection: Connection, *, identity: Identity, scope: str) -> None:
    link = await serve(connection, identity=identity, scope=scope)
    if link is None:
        return
    async with Session(link, video=True) as screen:
        screens.append(screen)
        try:
            async for _ in screen.events():
                pass
        finally:
            screens.remove(screen)


async def ask(question: str) -> dict:
    screen = screens[-1]
    return await harness.run(question, screen.scope, SensesTools(screen, triggered_by=REVIEWER))

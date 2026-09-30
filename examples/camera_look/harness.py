from rfnry_engine import Engine
from rfnry_senses import Identity, SensesTools, Session

from providers import AnthropicProvider

INSPECTOR = Identity(role="assistant", id="inspector", name="Inspector")
engine = Engine("agents", generation=AnthropicProvider())


async def ask(session: Session, question: str) -> str:
    eyes = SensesTools(session, triggered_by=INSPECTOR)
    run = await engine.run("inspector", scope=session.scope, input=question, tools=eyes)
    return run.reply

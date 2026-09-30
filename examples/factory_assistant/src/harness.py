from pathlib import Path

from rfnry_engine import Engine, Message
from rfnry_senses import SensesTools

from providers import AnthropicProvider

ROOT = Path(__file__).resolve().parent.parent
PLANT = (ROOT / "data" / "plant.json").read_text()

engine = Engine(ROOT / "agents", generation=AnthropicProvider())


async def run(question: str, scope: str, history: list[Message], senses: SensesTools) -> str:
    engine_run = await engine.run(
        "maintenance",
        scope=scope,
        input=question,
        history=history,
        context=PLANT,
        tools=senses.only("look"),
    )
    return engine_run.reply

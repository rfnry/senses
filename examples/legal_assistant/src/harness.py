from pathlib import Path

from rfnry_engine import Engine
from rfnry_senses import SensesTools

from providers import AnthropicProvider

ROOT = Path(__file__).resolve().parent.parent

engine = Engine(ROOT / "agents", generation=AnthropicProvider())


async def run(question: str, scope: str, senses: SensesTools) -> dict:
    engine_run = await engine.run(
        "reviewer", scope=scope, input=question, tools=senses.only("look")
    )
    return {"reply": engine_run.reply, "run_id": engine_run.run_id}

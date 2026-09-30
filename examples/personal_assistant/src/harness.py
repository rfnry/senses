from datetime import date
from pathlib import Path

from rfnry_engine import Engine, Tools
from rfnry_senses import SensesTools

from providers import AnthropicProvider

ROOT = Path(__file__).resolve().parent.parent

engine = Engine(ROOT / "agents", generation=AnthropicProvider())


def consult(scope: str, senses: SensesTools) -> Tools:
    return engine.tool(
        "assistant",
        scope=scope,
        name="consult",
        description="Ask the mind behind you. It sees the camera and knows the home's paperwork.",
        context=f"Today is {date.today().isoformat()}.",
        tools=senses.only("look", "inform"),
    )

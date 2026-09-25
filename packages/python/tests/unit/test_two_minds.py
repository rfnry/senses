from typing import Any

import pytest
from pydantic import BaseModel

from rfnry_senses.contracts import LiveToolCall
from rfnry_senses.errors import TransportError
from rfnry_senses.events import Looked, ToolCalled
from rfnry_senses.models import Image, Text
from rfnry_senses.senses_tools import SensesTools
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools, ToolsLike
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, JPEG, ScriptedLive, next_of, until


class Ask(BaseModel):
    input: str


class ScriptedMind:
    def __init__(self, tools: ToolsLike) -> None:
        self.tools = tools
        self.seen: list[Any] = []

    async def run(self, question: str) -> str:
        self.seen = list(await self.tools.dispatch("look", {"frames": 1}))
        return f"{question} A desk, {self.seen[1].text}."


def agent_tool(name: str, mind: ScriptedMind) -> Tools:
    async def body(args: dict[str, Any]) -> str:
        return await mind.run(args["input"])

    return Tools([Tool(name, "Ask the mind behind you.", body=body, args=Ask)])


async def test_the_consulted_mind_looks_through_tools_built_before_start() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    session = Session(link, live=live, video=True)
    mind = ScriptedMind(SensesTools(session, triggered_by=HOST))
    await session.start(tools=agent_tool("consult", mind))
    async with session:
        events = session.events()
        await link.show(JPEG)
        await until(lambda: bool(live.connection.images))
        live.connection.emit(
            LiveToolCall(call_id="c1", name="consult", args={"input": "What is there?"})
        )
        looked = await next_of(events, Looked)
        called = await next_of(events, ToolCalled)
    assert [spec.name for spec in live.opened["tools"]] == ["consult"]
    assert looked.triggered_by == HOST and looked.frames == 1
    assert isinstance(mind.seen[0], Image) and isinstance(mind.seen[1], Text)
    assert not called.is_error and isinstance(called.output, str)
    assert called.output.startswith("What is there? A desk, frame at ")
    assert live.connection.results == [("c1", called.output, False)]


async def test_senses_tools_built_before_start_offer_the_room_and_wait_for_it() -> None:
    session = Session(Loopback(ANA, "acme"), live=ScriptedLive(), video=True)
    tools = SensesTools(session, triggered_by=HOST)
    assert [d["name"] for d in tools.definitions()] == [
        "look",
        "say",
        "prompt",
        "inform",
        "interrupt",
    ]
    with pytest.raises(TransportError, match="^session: start the session before using it$"):
        await tools.dispatch("look", {})

import asyncio

import pytest

from rfnry_senses.errors import InputError, NotFoundError
from rfnry_senses.models import Image, Text
from rfnry_senses.senses_tools import SensesTools
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, JPEG, ScriptedLive, ScriptedTranscription, Tone, response, until


def names(tools: SensesTools) -> list[str]:
    return [d["name"] for d in tools.definitions()]


async def test_the_tools_offered_follow_what_the_session_can_do() -> None:
    async with Session(Loopback(ANA, "acme"), live=ScriptedLive(), video=True) as live:
        assert names(SensesTools(live, triggered_by=HOST)) == [
            "look",
            "say",
            "prompt",
            "inform",
            "interrupt",
        ]
    async with Session(
        Loopback(ANA, "acme"), transcription=ScriptedTranscription(), speech=Tone()
    ) as cascaded:
        assert names(SensesTools(cascaded, triggered_by=HOST)) == ["say", "interrupt"]
    async with Session(Loopback(ANA, "acme"), video=True) as camera:
        assert names(SensesTools(camera, triggered_by=HOST)) == ["look"]


async def test_the_tools_act_for_their_cause() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with Session(link, live=live, video=True) as s:
        tools = SensesTools(s, triggered_by=HOST)
        await link.show(JPEG)
        await asyncio.sleep(0.01)
        seen = await tools.dispatch("look", {})
        saying = asyncio.create_task(tools.dispatch("say", {"text": "Hello."}))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(*response("Hello."))
        said = await saying
        prompting = asyncio.create_task(tools.dispatch("prompt", {"text": "Greet Ana."}))
        await until(lambda: len(live.connection.calls) == 2)
        live.connection.emit(*response("Hi Ana."))
        prompted = await prompting
        informed = await tools.dispatch("inform", {"text": "Ana is vegetarian."})
        cut = await tools.dispatch("interrupt", {})
        with pytest.raises(InputError, match="^tools: invalid arguments for say"):
            await tools.dispatch("say", {})
    assert isinstance(seen, list) and isinstance(seen[0], Image) and isinstance(seen[1], Text)
    assert said == {"heard": "Hello.", "interrupted": False}
    assert prompted == {"said": "Hi Ana.", "heard": "Hi Ana.", "interrupted": False}
    assert informed == "informed" and cut == {"heard": ""}


async def test_a_failing_tool_raises_the_librarys_error() -> None:
    async with Session(Loopback(ANA, "acme"), video=True) as s:
        with pytest.raises(NotFoundError, match="^session: no frame in the last 10 s$"):
            await SensesTools(s, triggered_by=HOST).dispatch("look", {"frames": 2})

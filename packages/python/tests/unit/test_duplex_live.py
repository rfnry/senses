import asyncio
from typing import Any

import pytest

from rfnry_senses.contracts import LiveAudio, LiveDone, LiveSaid, LiveStarted
from rfnry_senses.errors import ModelError, TransportError
from rfnry_senses.events import Informed, Interrupted
from rfnry_senses.senses_tools import SensesTools
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .fakes import (
    ANA,
    HOST,
    DuplexConnection,
    DuplexLive,
    ScriptedConnection,
    ScriptedLive,
    next_of,
    pcm,
    until,
)


def stopped(link: Loopback) -> bool:
    return any(control["type"] == "stop" for control in link.controls)


async def speaking(link: Loopback, live: ScriptedLive) -> None:
    live.connection.emit(LiveStarted(requested=False), LiveAudio(frame=pcm(100)))
    await until(lambda: bool(link.audio))


class HeldSend(DuplexConnection):
    def __init__(self) -> None:
        super().__init__()
        self.release = asyncio.Event()

    async def send_text(self, text: str, *, respond: bool) -> str | None:
        await self.release.wait()
        return await super().send_text(text, respond=respond)


class NoTagOnSay(DuplexConnection):
    async def say(self, text: str) -> str | None:
        return await ScriptedConnection.say(self, text)


class Undeclared:
    async def open(self, *, tools: Any, instructions: str | None) -> Any:
        return object()


async def test_a_prompt_goes_while_a_response_is_open() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with Session(link, live=live) as s:
        await speaking(link, live)
        said = await asyncio.wait_for(s.prompt("the news", triggered_by=HOST), 1)
    assert said is None
    assert ("text", "the news", True) in live.connection.calls


async def test_a_prompt_with_no_pair_reports_informed() -> None:
    live = DuplexLive()
    async with Session(Loopback(ANA, "acme"), live=live) as s:
        events = s.events()
        await asyncio.wait_for(s.prompt("the news", triggered_by=HOST), 1)
        informed = await next_of(events, Informed)
    assert informed.text == "the news" and informed.triggered_by == HOST


async def test_nothing_waits_behind_a_prompt_with_no_pair() -> None:
    live = DuplexLive()
    async with Session(Loopback(ANA, "acme"), live=live) as s, asyncio.timeout(1):
        await s.prompt("one", triggered_by=HOST)
        await s.prompt("two", triggered_by=HOST)
        await s.inform("three", triggered_by=HOST)
    assert live.connection.calls == [
        ("text", "one", True),
        ("text", "two", True),
        ("text", "three", False),
    ]


async def test_quiet_returns_once_a_prompt_with_no_pair_has_gone() -> None:
    live = DuplexLive()
    live.connection = connection = HeldSend()
    async with Session(Loopback(ANA, "acme"), live=live) as s:
        prompting = asyncio.create_task(s.prompt("the news", triggered_by=HOST))
        quiet = asyncio.create_task(s.quiet())
        await asyncio.sleep(0.01)
        assert not quiet.done()
        connection.release.set()
        await asyncio.wait_for(quiet, 1)
        assert await prompting is None


async def test_a_tagged_prompt_waits_for_its_response() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    live.connection.tagging = True
    async with Session(link, live=live) as s:
        prompting = asyncio.create_task(s.prompt("the news", triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        await asyncio.sleep(0.01)
        assert not prompting.done()
        live.connection.emit(
            LiveStarted(requested="request-1"),
            LiveAudio(frame=pcm(20)),
            LiveSaid(text="Here it is. "),
            LiveDone(),
        )
        said = await asyncio.wait_for(prompting, 1)
    assert said is not None
    assert said.text == "Here it is. " and said.triggered_by == HOST


async def test_a_say_goes_while_a_response_is_open() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with Session(link, live=live) as s:
        await speaking(link, live)
        saying = asyncio.create_task(s.say("Exactly this.", triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        assert live.connection.calls == [("say", "Exactly this.")]
        assert not saying.done()
        live.connection.emit(
            LiveDone(),
            LiveStarted(requested="request-1"),
            LiveSaid(text="Exactly this."),
            LiveAudio(frame=pcm(20)),
            LiveDone(),
        )
        said = await asyncio.wait_for(saying, 1)
    assert said.heard == said.text == "Exactly this."


async def test_a_say_with_no_tag_raises() -> None:
    live = DuplexLive()
    live.connection = NoTagOnSay()
    async with Session(Loopback(ANA, "acme"), live=live) as s:
        with pytest.raises(ModelError, match="^session: a say on a duplex model needs a tag$"):
            await asyncio.wait_for(s.say("Exactly this.", triggered_by=HOST), 1)
        await asyncio.wait_for(s.quiet(), 1)


async def test_input_waits_for_a_cut_to_finish() -> None:
    link, live = Loopback(ANA, "acme", answer=False), DuplexLive()
    async with Session(link, live=live) as s:
        await speaking(link, live)
        events = s.events()
        cutting = asyncio.create_task(s.interrupt(triggered_by=HOST))
        await until(lambda: stopped(link))
        prompting = asyncio.create_task(s.prompt("the news", triggered_by=HOST))
        await asyncio.sleep(0.05)
        assert live.connection.calls == []
        await next_of(events, Interrupted)
        assert await asyncio.wait_for(prompting, 1) is None
        await cutting
    assert live.connection.calls == [("text", "the news", True)]


async def test_an_inform_waits_for_a_cut_to_finish() -> None:
    link, live = Loopback(ANA, "acme", answer=False), DuplexLive()
    async with Session(link, live=live) as s:
        await speaking(link, live)
        events = s.events()
        cutting = asyncio.create_task(s.interrupt(triggered_by=HOST))
        await until(lambda: stopped(link))
        informing = asyncio.create_task(s.inform("the news", triggered_by=HOST))
        await asyncio.sleep(0.05)
        assert live.connection.calls == []
        await next_of(events, Interrupted)
        await asyncio.wait_for(informing, 1)
        await cutting
    assert live.connection.calls == [("text", "the news", False)]


async def test_an_inform_waiting_for_a_cut_raises_when_the_session_closes() -> None:
    link, live = Loopback(ANA, "acme", answer=False), DuplexLive()
    s = Session(link, live=live)
    await s.start()
    await speaking(link, live)
    cutting = asyncio.create_task(s.interrupt(triggered_by=HOST))
    await until(lambda: stopped(link))
    informing = asyncio.create_task(s.inform("the news", triggered_by=HOST))
    await asyncio.sleep(0.01)
    await s.close()
    with pytest.raises(TransportError, match="^session: the session is closed$"):
        await asyncio.wait_for(informing, 1)
    with pytest.raises(TransportError):
        await cutting
    assert live.connection.calls == []


async def test_the_prompt_tool_says_prompted_with_no_pair() -> None:
    async with Session(Loopback(ANA, "acme"), live=DuplexLive()) as s:
        tools = SensesTools(s, triggered_by=HOST)
        assert await asyncio.wait_for(tools.dispatch("prompt", {"text": "x"}), 1) == "prompted"


async def test_a_turn_based_prompt_still_waits_behind_a_response() -> None:
    link, live = Loopback(ANA, "acme"), ScriptedLive()
    async with Session(link, live=live) as s:
        await speaking(link, live)
        prompting = asyncio.create_task(s.prompt("the news", triggered_by=HOST))
        await asyncio.sleep(0.01)
        assert live.connection.calls == []
        live.connection.emit(LiveDone())
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(LiveStarted(), LiveSaid(text="Here. "), LiveDone())
        said = await asyncio.wait_for(prompting, 1)
    assert said is not None and said.text == "Here. "


async def test_a_turn_based_inform_goes_during_a_cut() -> None:
    link, live = Loopback(ANA, "acme", answer=False), ScriptedLive()
    async with Session(link, live=live) as s:
        await speaking(link, live)
        cutting = asyncio.create_task(s.interrupt(triggered_by=HOST))
        await until(lambda: stopped(link))
        await asyncio.wait_for(s.inform("the news", triggered_by=HOST), 0.1)
        assert not cutting.done()
        await cutting


async def test_a_connection_that_does_not_declare_duplex_does_not_start() -> None:
    s = Session(Loopback(ANA, "acme"), live=Undeclared())
    with pytest.raises(AttributeError, match="duplex"):
        await s.start()
    await s.close()

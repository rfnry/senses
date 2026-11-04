import asyncio
from typing import Any

import pytest

from rfnry_senses.contracts import (
    LiveAudio,
    LiveDone,
    LiveInterrupted,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
    LiveToolCancelled,
)
from rfnry_senses.errors import TransportError
from rfnry_senses.events import Closed, Event, Interrupted, Said, Silent, Speaking, ToolCalled
from rfnry_senses.models import ToolSpec
from rfnry_senses.senses_tools import SensesTools
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, ScriptedConnection, ScriptedLive, SlowLoopback, pcm, until


def recording(s: Session) -> tuple[list[Event], asyncio.Task[None]]:
    seen: list[Event] = []
    events = s.events()

    async def record() -> None:
        async for event in events:
            seen.append(event)

    return seen, asyncio.create_task(record())


async def the_cut_finished(
    s: Session, connection: ScriptedConnection, seen: list[Event], asked: asyncio.Task[Said]
) -> None:
    said = await asyncio.wait_for(asked, 1)
    await asyncio.wait_for(s.quiet(), 1)
    assert len(connection.interrupts) == 1
    assert said.interrupted and said in seen
    assert any(isinstance(e, Interrupted) for e in seen)
    seen.clear()
    connection.emit(
        LiveStarted(), LiveSaid(text="Hello? "), LiveAudio(frame=pcm(50, 24000)), LiveDone()
    )
    await until(lambda: any(isinstance(e, Silent) for e in seen))
    assert [type(e) for e in seen if isinstance(e, Speaking | Silent)] == [Speaking, Silent]


async def test_a_given_up_interrupt_still_finishes_the_cut() -> None:
    live, link = ScriptedLive(), SlowLoopback(ANA, "acme", delay=0.3)
    async with Session(link, live=live) as s:
        seen, recorder = recording(s)
        asked = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(
            LiveStarted(requested=True),
            LiveSaid(text="Your table "),
            LiveAudio(frame=pcm(200, 24000)),
            LiveDone(),
        )
        await until(lambda: len(link.audio) == 1)
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(s.interrupt(triggered_by=HOST), 0.05)
        await the_cut_finished(s, live.connection, seen, asked)
    await recorder


async def test_a_withdrawn_interrupt_tool_still_finishes_the_cut() -> None:
    live, link = ScriptedLive(), SlowLoopback(ANA, "acme", delay=0.3)
    s = Session(link, live=live)
    await s.start(tools=SensesTools(s, triggered_by=HOST).only("interrupt"))
    async with s:
        seen, recorder = recording(s)
        asked = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(
            LiveStarted(requested=True),
            LiveSaid(text="Your table "),
            LiveAudio(frame=pcm(200, 24000)),
            LiveToolCall(call_id="c1", name="interrupt"),
        )
        await until(lambda: {"type": "stop", "utterance": 1} in link.controls)
        live.connection.emit(LiveToolCancelled(call_ids=["c1"]))
        await until(lambda: any(isinstance(e, ToolCalled) for e in seen))
        live.connection.emit(LiveDone())
        await the_cut_finished(s, live.connection, seen, asked)
    await recorder
    assert live.connection.results == []


@pytest.mark.parametrize("end", ["hang_up", "close"])
async def test_closing_during_a_cut_fails_the_say_it_took(end: str) -> None:
    live, link = ScriptedLive(), SlowLoopback(ANA, "acme", delay=None)
    s = Session(link, live=live)
    await s.start()
    live.connection.emit(
        LiveStarted(), LiveSaid(text="Welcome. "), LiveAudio(frame=pcm(100, 24000)), LiveDone()
    )
    await until(lambda: len(link.audio) == 1)
    asked = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
    await until(lambda: live.connection.calls == [("say", "Your table is ready.")])
    live.connection.emit(LiveStarted(requested=True), LiveInterrupted())
    await until(lambda: {"type": "stop", "utterance": 1} in link.controls)
    if end == "hang_up":
        await link.hang_up()
    else:
        await s.close()
    with pytest.raises(TransportError, match="^session: the session closed before the reply"):
        await asyncio.wait_for(asked, 1)
    await s.close()


class SlowClose(ScriptedConnection):
    async def close(self) -> None:
        await asyncio.sleep(0.05)
        await super().close()


async def holding_a_say(link: Loopback, live: ScriptedLive) -> tuple[Session, asyncio.Task[Said]]:
    s = Session(link, live=live)
    await s.start()
    live.connection.emit(LiveStarted(), LiveAudio(frame=pcm(100, 24000)))
    await until(lambda: len(link.audio) == 1)
    held = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await asyncio.sleep(0.01)
    return s, held


async def test_a_cancelled_close_still_closes_everything() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = SlowClose()
    s, held = await holding_a_say(link, live)
    seen, recorder = recording(s)

    async def close_cancelled_twice() -> None:
        try:
            await s.close()
        finally:
            await s.close()

    closing = asyncio.create_task(close_cancelled_twice())
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0.01)
    closing.cancel()
    await asyncio.gather(closing, return_exceptions=True)
    assert closing.cancelled()
    await asyncio.wait_for(recorder, 1)
    assert live.connection.closed and link.closed
    assert isinstance(seen[-1], Closed) and seen[-1].reason == "closed"
    with pytest.raises(TransportError, match="^session: the session closed before the reply"):
        await asyncio.wait_for(held, 1)


async def test_a_second_close_waits_for_the_first() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = SlowClose()
    s, held = await holding_a_say(link, live)
    first = asyncio.create_task(s.close())
    await asyncio.sleep(0)
    await s.close()
    assert live.connection.closed and link.closed
    await first
    with pytest.raises(TransportError):
        await held


class GatedLive(ScriptedLive):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> ScriptedConnection:
        await self.gate.wait()
        return await super().open(tools=tools, instructions=instructions)


async def test_close_during_start_closes_the_new_connection() -> None:
    live, link = GatedLive(), Loopback(ANA, "acme")
    s = Session(link, live=live)
    seen, recorder = recording(s)
    starting = asyncio.create_task(s.start())
    await asyncio.sleep(0.01)
    await s.close()
    live.gate.set()
    with pytest.raises(TransportError, match="^session: the session is closed$"):
        await asyncio.wait_for(starting, 1)
    await asyncio.wait_for(recorder, 1)
    assert live.connection.closed
    assert [type(e) for e in seen] == [Closed]
    assert {"type": "ready"} not in link.controls
    await asyncio.sleep(0.01)
    assert asyncio.all_tasks() == {asyncio.current_task()}


class Down(ScriptedLive):
    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> ScriptedConnection:
        raise ConnectionError("vendor unreachable")


async def test_a_failed_start_in_async_with_closes_the_transport() -> None:
    link = Loopback(ANA, "acme")
    s = Session(link, live=Down())
    seen, recorder = recording(s)
    with pytest.raises(ConnectionError, match="^vendor unreachable$"):
        async with s:
            pass
    await asyncio.wait_for(recorder, 1)
    assert link.closed
    assert isinstance(seen[-1], Closed) and seen[-1].reason == "failed"


class DownOnce(ScriptedLive):
    def __init__(self) -> None:
        super().__init__()
        self.down = True

    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> ScriptedConnection:
        if self.down:
            self.down = False
            raise ConnectionError("vendor unreachable")
        return await super().open(tools=tools, instructions=instructions)


async def test_an_explicit_start_that_raises_can_be_retried() -> None:
    live, link = DownOnce(), Loopback(ANA, "acme")
    s = Session(link, live=live)
    with pytest.raises(ConnectionError):
        await s.start()
    assert not link.closed
    await s.start()
    await s.close()
    assert link.controls[0] == {"type": "ready"} and live.connection.closed


class CountedConnection(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1
        await super().close()


class CountedLink(SlowLoopback):
    def __init__(self) -> None:
        super().__init__(ANA, "acme", delay=None)
        self.closes = 0

    async def close(self) -> None:
        self.closes += 1
        await super().close()


async def test_a_hang_up_during_a_cut_with_a_closing_tool_resolves_everything() -> None:
    live, link = ScriptedLive(), CountedLink()
    live.connection = connection = CountedConnection()
    s = Session(link, live=live)
    gate, ended = asyncio.Event(), asyncio.Event()

    async def end_call(args: dict[str, Any]) -> str:
        await gate.wait()
        try:
            await s.close()
        finally:
            ended.set()
        return "ended"

    await s.start(tools=Tools([Tool("end_call", "End", body=end_call)]), inform=["end_call"])
    seen, recorder = recording(s)
    connection.emit(
        LiveStarted(),
        LiveSaid(text="Welcome. "),
        LiveAudio(frame=pcm(100, 24000)),
        LiveToolCall(call_id="c1", name="end_call"),
        LiveDone(),
    )
    await until(lambda: len(link.audio) == 1)
    cut = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
    await until(lambda: len(connection.calls) == 1)
    held = asyncio.create_task(s.say("Shall I order?", triggered_by=HOST))
    connection.emit(LiveStarted(requested=True), LiveInterrupted())
    await until(lambda: {"type": "stop", "utterance": 1} in link.controls)
    gate.set()
    await link.hang_up()
    outcomes = await asyncio.wait_for(asyncio.gather(cut, held, return_exceptions=True), 1)
    await asyncio.wait_for(ended.wait(), 1)
    await asyncio.wait_for(recorder, 1)
    assert all(isinstance(outcome, TransportError) for outcome in outcomes)
    assert [type(e) for e in seen].count(Closed) == 1 and isinstance(seen[-1], Closed)
    assert connection.closes == 1 and link.closes == 1
    assert connection.results == []

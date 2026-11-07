import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from rfnry_senses.contracts import (
    AudioFrame,
    LiveAudio,
    LiveDone,
    LiveFailed,
    LiveInterrupted,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
    LiveToolCancelled,
)
from rfnry_senses.errors import ModelError, TransportError
from rfnry_senses.events import (
    Closed,
    Event,
    Interrupted,
    Muted,
    Said,
    Silent,
    Speaking,
    ToolCalled,
)
from rfnry_senses.models import ToolSpec
from rfnry_senses.senses_tools import SensesTools
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools
from rfnry_senses.transport import Loopback

from .fakes import (
    ANA,
    HOST,
    ScriptedConnection,
    ScriptedLive,
    SlowLoopback,
    Tone,
    pcm,
    response,
    until,
)


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
    await until(lambda: link.closed)
    assert live.connection.closed
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
    outcome, queued = await asyncio.wait_for(asyncio.gather(cut, held, return_exceptions=True), 1)
    await asyncio.wait_for(ended.wait(), 1)
    await asyncio.wait_for(recorder, 1)
    await until(lambda: link.closes == 1)
    assert isinstance(outcome, TransportError)
    assert isinstance(queued, Said) and queued.interrupted and queued.heard == ""
    assert [type(e) for e in seen].count(Closed) == 1 and isinstance(seen[-1], Closed)
    assert connection.closes == 1 and link.closes == 1
    assert connection.results == []


async def test_a_tool_that_closes_as_the_close_stops_it_does_not_hold_the_close() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = Session(link, live=live)
    running = asyncio.Event()

    async def end_call(args: dict[str, Any]) -> str:
        running.set()
        try:
            await asyncio.sleep(10)
        finally:
            await s.close()
        return "ended"

    await s.start(tools=Tools([Tool("end_call", "End", body=end_call)]))
    seen, recorder = recording(s)
    live.connection.emit(LiveToolCall(call_id="c1", name="end_call"))
    await asyncio.wait_for(running.wait(), 1)
    await link.hang_up()
    await asyncio.wait_for(recorder, 1)
    await until(lambda: link.closed)
    assert isinstance(seen[-1], Closed) and seen[-1].reason == "hung_up"
    assert live.connection.closed


def event(kind: str, **fields: Any) -> Callable[[dict[str, Any]], bool]:
    def matches(control: dict[str, Any]) -> bool:
        if control["type"] != "event" or control["event"]["kind"] != kind:
            return False
        return all(control["event"][name] == value for name, value in fields.items())

    return matches


class GatedLink(Loopback):
    def __init__(self, held: Callable[[dict[str, Any]], bool]) -> None:
        super().__init__(ANA, "acme")
        self.held = held
        self.gate = asyncio.Event()
        self.at_gate = False

    async def send_control(self, control: dict[str, Any]) -> None:
        if self.held(control):
            self.at_gate = True
            await self.gate.wait()
        await super().send_control(control)


async def test_a_close_while_an_utterance_goes_silent_still_ends_its_say() -> None:
    live, link = ScriptedLive(), GatedLink(event("silent"))
    s = Session(link, live=live)
    await s.start()
    asked = asyncio.create_task(s.say("Hi.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 1)
    live.connection.emit(*response("Hi. "))
    await until(lambda: link.at_gate)
    await s.close()
    said = await asyncio.wait_for(asked, 1)
    link.gate.set()
    assert said.heard == "Hi. " and not said.interrupted


class BreaksAt(Tone):
    def __init__(self, sentence: str) -> None:
        super().__init__()
        self.breaks = sentence

    async def speak(self, text: str) -> AsyncIterator[AudioFrame]:
        if text == self.breaks:
            raise RuntimeError("synthesis failed")
        async for frame in super().speak(text):
            yield frame


async def test_a_close_while_a_failed_utterance_goes_silent_still_fails_its_say() -> None:
    link = GatedLink(event("silent"))
    s = Session(link, speech=BreaksAt("Two."))
    await s.start()
    asked = asyncio.create_task(s.say("One. Two.", triggered_by=HOST))
    await until(lambda: link.at_gate)
    await s.close()
    with pytest.raises(ModelError, match="^session: the speech model failed"):
        await asyncio.wait_for(asked, 1)
    link.gate.set()


async def test_a_close_while_a_barge_in_reports_the_requests_it_ended_strands_nothing() -> None:
    live, link = ScriptedLive(), GatedLink(event("said", text="First."))
    s = Session(link, live=live)
    await s.start()
    first = asyncio.create_task(s.say("First.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 1)
    second = asyncio.create_task(s.say("Second.", triggered_by=HOST))
    await asyncio.sleep(0.01)
    live.connection.emit(LiveInterrupted())
    await until(lambda: link.at_gate)
    await s.close()
    ended = await asyncio.wait_for(asyncio.gather(first, second), 1)
    link.gate.set()
    assert all(said.interrupted and said.heard == "" for said in ended)
    assert live.connection.calls == [("say", "First.")]


class HeldInterrupt(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.at_gate = False

    async def interrupt(self, played_ms: int, *, heard: str, response: str | None) -> None:
        self.at_gate = True
        await self.gate.wait()
        await super().interrupt(played_ms, heard=heard, response=response)


async def test_a_close_while_a_muted_response_is_cut_fails_its_say() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = connection = HeldInterrupt()
    s = Session(link, live=live)
    await s.start()
    chunks: asyncio.Queue[str] = asyncio.Queue()

    async def reply() -> AsyncIterator[str]:
        yield await chunks.get()

    asked = asyncio.create_task(s.say(reply(), triggered_by=HOST))
    await asyncio.sleep(0.01)
    await s.mute(triggered_by=HOST)
    chunks.put_nowait("Hello.")
    await until(lambda: len(connection.calls) == 1)
    connection.emit(LiveStarted(requested=True))
    await until(lambda: connection.at_gate)
    await s.close()
    with pytest.raises(TransportError, match="^session: the session closed before the reply"):
        await asyncio.wait_for(asked, 1)
    connection.gate.set()


class DeadLine(Loopback):
    async def send_control(self, control: dict[str, Any]) -> None:
        if control["type"] == "speaking":
            raise ConnectionError("line down")
        await super().send_control(control)

    def clear_audio(self) -> None:
        raise ConnectionError("line down")


async def test_a_say_ends_when_speaking_and_clearing_both_fail() -> None:
    link = DeadLine(ANA, "acme")
    s = Session(link, speech=Tone())
    await s.start()
    seen, recorder = recording(s)
    with pytest.raises(TransportError, match="^session: the transport failed: line down$"):
        await asyncio.wait_for(s.say("Hello there.", triggered_by=HOST), 1)
    await asyncio.wait_for(recorder, 1)
    assert isinstance(seen[-1], Closed) and seen[-1].reason == "failed"


class TwoFaults(Loopback):
    async def send_audio(self, frame: AudioFrame, utterance: int) -> None:
        raise ConnectionError("the audio went first")

    async def send_control(self, control: dict[str, Any]) -> None:
        if control["type"] == "silent":
            raise ConnectionError("the control went after")
        await super().send_control(control)


async def test_a_failure_cleaning_up_after_a_transport_failure_keeps_the_first_error() -> None:
    link = TwoFaults(ANA, "acme")
    s = Session(link, speech=Tone())
    await s.start()
    seen, recorder = recording(s)
    with pytest.raises(TransportError, match="the audio went first"):
        await asyncio.wait_for(s.say("Hello there.", triggered_by=HOST), 1)
    await asyncio.wait_for(recorder, 1)
    assert isinstance(seen[-1], Closed) and "the audio went first" in (seen[-1].message or "")


class Stubborn:
    def __init__(self, session: Session, way: str) -> None:
        self.session = session
        self.way = way
        self.running = asyncio.Event()
        self.released = asyncio.Event()

    async def body(self, args: dict[str, Any]) -> str:
        self.running.set()
        try:
            await asyncio.sleep(30)
        finally:
            if self.way == "shield":
                await asyncio.shield(self.session.close())
            elif self.way == "group":
                async with asyncio.TaskGroup() as group:
                    group.create_task(self.session.close())
            else:
                while not self.released.is_set():
                    with contextlib.suppress(asyncio.CancelledError):
                        await self.released.wait()
        return "ended"


@pytest.mark.parametrize("way", ["shield", "group", "ignores"])
async def test_a_tool_that_will_not_stop_holds_a_close_only_for_the_grace(
    monkeypatch: pytest.MonkeyPatch, way: str
) -> None:
    monkeypatch.setattr("rfnry_senses.session.CLOSE_GRACE", 0.05)
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = Session(link, live=live)
    tool = Stubborn(s, way)
    await s.start(tools=Tools([Tool("stubborn", "Stubborn", body=tool.body)]))
    seen, recorder = recording(s)
    asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 1)
    live.connection.emit(LiveToolCall(call_id="c1", name="stubborn"))
    await asyncio.wait_for(tool.running.wait(), 1)
    await asyncio.wait_for(s.close(), 1)
    await asyncio.wait_for(recorder, 1)
    tool.released.set()
    with pytest.raises(TransportError, match="^session: the session closed before the reply"):
        await asyncio.wait_for(asked, 1)
    assert isinstance(seen[-1], Closed) and live.connection.closed and link.closed


async def test_a_close_fails_its_waiters_before_it_waits_for_what_it_stopped() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = Session(link, live=live)
    tool = Stubborn(s, "ignores")
    await s.start(tools=Tools([Tool("stubborn", "Stubborn", body=tool.body)]))
    asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 1)
    live.connection.emit(LiveToolCall(call_id="c1", name="stubborn"))
    await asyncio.wait_for(tool.running.wait(), 1)
    closing = asyncio.create_task(s.close())
    await asyncio.wait({asked}, timeout=1)
    failed_first = asked.done() and not closing.done() and not live.connection.closed
    tool.released.set()
    await asyncio.wait_for(closing, 1)
    with pytest.raises(TransportError, match="^session: the session closed before the reply"):
        await asked
    assert failed_first


async def test_a_send_that_hangs_on_closed_holds_a_close_only_for_the_grace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rfnry_senses.session.CLOSE_GRACE", 0.05)
    link = GatedLink(event("closed"))
    s = Session(link, speech=Tone())
    await s.start()
    seen, recorder = recording(s)
    await asyncio.wait_for(s.close(), 1)
    await asyncio.wait_for(recorder, 1)
    assert isinstance(seen[-1], Closed) and link.closed


class HungClosed(Loopback):
    def __init__(self) -> None:
        super().__init__(ANA, "acme")
        self.cancelled = False

    async def send_control(self, control: dict[str, Any]) -> None:
        if event("closed")(control):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
        await super().send_control(control)


async def test_a_closed_send_given_up_on_is_cancelled_and_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr("rfnry_senses.session.CLOSE_GRACE", 0.05)
    link = HungClosed()
    s = Session(link, speech=Tone())
    await s.start()
    await asyncio.wait_for(s.close(), 1)
    await until(lambda: link.cancelled)
    assert "session: the closed event was not sent within 0.05 s; the close goes on" in (
        caplog.messages
    )


async def test_the_events_end_while_the_closed_send_still_hangs() -> None:
    link = GatedLink(event("closed"))
    s = Session(link, speech=Tone())
    await s.start()
    seen, recorder = recording(s)
    closing = asyncio.create_task(s.close())
    await until(lambda: link.at_gate)
    await asyncio.wait_for(recorder, 1)
    link.gate.set()
    await closing
    assert isinstance(seen[-1], Closed)


class HeldInform(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def send_text(self, text: str, *, respond: bool) -> str | None:
        if not respond:
            await self.gate.wait()
        return await super().send_text(text, respond=respond)


async def test_closed_is_the_last_event_a_subscriber_sees() -> None:
    live, link = ScriptedLive(), GatedLink(event("closed"))
    live.connection = connection = HeldInform()
    s = Session(link, live=live)
    await s.start()
    seen, recorder = recording(s)
    informing = asyncio.create_task(s.inform("The terrace is closed.", triggered_by=HOST))
    await asyncio.sleep(0.01)
    closing = asyncio.create_task(s.close())
    await until(lambda: link.at_gate)
    connection.gate.set()
    await asyncio.wait_for(informing, 1)
    link.gate.set()
    await asyncio.wait_for(closing, 1)
    await asyncio.wait_for(recorder, 1)
    assert [type(e) for e in seen] == [Closed]


async def test_silent_and_said_reach_subscribers_together_before_the_say_returns() -> None:
    live, link = ScriptedLive(), GatedLink(event("silent"))
    s = Session(link, live=live)
    await s.start()
    seen, recorder = recording(s)
    asked = asyncio.create_task(s.say("Hi.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 1)
    live.connection.emit(*response("Hi. "))
    said = await asyncio.wait_for(asked, 1)
    await asyncio.sleep(0)
    told = [type(e) for e in seen]
    link.gate.set()
    await s.close()
    await recorder
    assert told == [Speaking, Silent, Said] and said in seen


async def test_a_stop_that_overtakes_a_slow_speaking_leaves_nothing_stuck() -> None:
    live, link = ScriptedLive(), GatedLink(lambda control: control == SPEAKING_FIRST)
    s = Session(link, live=live)
    await s.start()
    seen, recorder = recording(s)
    asked = asyncio.create_task(s.say("Hello there.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 1)
    live.connection.emit(*response("Hello there. "))
    await until(lambda: link.at_gate)
    await asyncio.wait_for(s.interrupt(triggered_by=HOST), 1)
    await asyncio.wait_for(asked, 1)
    link.gate.set()
    await asyncio.sleep(0.01)
    await asyncio.wait_for(s.quiet(), 1)
    before = [type(e) for e in seen if isinstance(e, Speaking | Silent)]
    again = asyncio.create_task(s.say("Again.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 2)
    live.connection.emit(*response("Again. "))
    await asyncio.wait_for(again, 1)
    await s.close()
    await recorder
    assert before == [Speaking, Silent]
    assert [type(e) for e in seen if isinstance(e, Speaking | Silent)] == [Speaking, Silent] * 2


SPEAKING_FIRST = {"type": "speaking", "utterance": 1}


async def test_a_slow_speaking_reports_nothing_after_the_silent_the_cut_reported() -> None:
    live, link = ScriptedLive(), GatedLink(lambda control: control == SPEAKING_FIRST)
    s = Session(link, live=live)
    await s.start()
    asked = asyncio.create_task(s.say("Hello there.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 1)
    live.connection.emit(*response("Hello there. "))
    await until(lambda: link.at_gate)
    await asyncio.wait_for(s.interrupt(triggered_by=HOST), 1)
    await asyncio.wait_for(asked, 1)
    link.gate.set()
    await asyncio.sleep(0.01)
    await s.close()
    kinds = [c["event"]["kind"] for c in link.controls if c["type"] == "event"]
    assert "speaking" not in kinds[kinds.index("silent") :]


async def test_a_cut_during_the_speaking_event_still_ends_in_silent() -> None:
    link = GatedLink(event("speaking"))
    s = Session(link, speech=Tone())
    await s.start()
    seen, recorder = recording(s)
    asked = asyncio.create_task(s.say("Hello there my friend.", triggered_by=HOST))
    await until(lambda: link.at_gate)
    await asyncio.wait_for(s.interrupt(triggered_by=HOST), 1)
    await asyncio.wait_for(asked, 1)
    link.gate.set()
    await asyncio.wait_for(s.quiet(), 1)
    await s.close()
    await recorder
    assert [type(e) for e in seen if isinstance(e, Speaking | Silent)] == [Speaking, Silent]


async def test_a_mute_given_up_at_its_event_still_cuts_the_voice() -> None:
    link, tts = GatedLink(event("muted")), Tone()
    tts.held = "Three four."
    s = Session(link, speech=tts)
    await s.start()
    seen, recorder = recording(s)
    asked = asyncio.create_task(s.say("One two. Three four.", triggered_by=HOST))
    await until(lambda: "Three four." in tts.spoken)
    muting = asyncio.create_task(s.mute(triggered_by=HOST))
    await until(lambda: link.at_gate)
    muting.cancel()
    await asyncio.gather(muting, return_exceptions=True)
    link.gate.set()
    said = await asyncio.wait_for(asked, 1)
    await asyncio.wait_for(s.quiet(), 1)
    await s.close()
    await recorder
    assert muting.cancelled() and said.interrupted
    assert {"type": "stop", "utterance": 1} in link.controls
    assert any(isinstance(e, Muted) for e in seen) and any(isinstance(e, Interrupted) for e in seen)


async def test_a_mute_given_up_while_it_ends_the_queue_still_ends_all_of_it() -> None:
    live, link = ScriptedLive(), GatedLink(event("said", text=""))
    s = Session(link, live=live)
    await s.start()
    first = asyncio.create_task(s.say("First.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 1)
    second = asyncio.create_task(s.prompt("Second.", triggered_by=HOST))
    third = asyncio.create_task(s.prompt("Third.", triggered_by=HOST))
    await asyncio.sleep(0.01)
    muting = asyncio.create_task(s.mute(triggered_by=HOST))
    await until(lambda: link.at_gate)
    muting.cancel()
    await asyncio.gather(muting, return_exceptions=True)
    link.gate.set()
    ended = await asyncio.wait_for(asyncio.gather(first, second, third), 1)
    await asyncio.wait_for(s.quiet(), 1)
    await s.close()
    assert all(said is not None and said.interrupted and said.heard == "" for said in ended)


class Blocking(ScriptedLive):
    def __init__(self) -> None:
        super().__init__()
        self.opening = asyncio.Event()

    async def open(self, *, tools: list[ToolSpec], instructions: str | None) -> ScriptedConnection:
        self.opening.set()
        await asyncio.sleep(30)
        return self.connection


async def test_a_cancelled_start_in_async_with_closes_the_transport() -> None:
    live, link = Blocking(), Loopback(ANA, "acme")
    s = Session(link, live=live)
    seen, recorder = recording(s)

    async def handler() -> None:
        async with s:
            raise AssertionError("the body runs only after a start")

    running = asyncio.create_task(handler())
    await asyncio.wait_for(live.opening.wait(), 1)
    running.cancel()
    await asyncio.gather(running, return_exceptions=True)
    await asyncio.wait_for(recorder, 1)
    assert running.cancelled() and link.closed
    assert isinstance(seen[-1], Closed) and seen[-1].reason == "closed"
    assert event("closed")(link.controls[-1])
    with pytest.raises(TransportError, match="^session: the session is closed$"):
        await s.start()


async def test_a_deadline_on_a_start_in_async_with_closes_the_transport() -> None:
    link = Loopback(ANA, "acme")
    s = Session(link, live=Blocking())
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01), s:
            raise AssertionError("the body runs only after a start")
    assert link.closed


class StopFails(SlowLoopback):
    async def send_control(self, control: dict[str, Any]) -> None:
        if control["type"] == "stop":
            raise ConnectionError("line down")
        await super().send_control(control)


async def test_a_failed_stop_fails_the_calls_it_cut() -> None:
    link = StopFails(ANA, "acme", delay=None)
    s = Session(link, speech=Tone(ms_per_word=200))
    await s.start()
    seen, recorder = recording(s)
    asked = asyncio.create_task(s.say("One two. Three four. Five six.", triggered_by=HOST))
    await until(lambda: any(c["type"] == "silent" for c in link.controls))
    with pytest.raises(TransportError, match="^session: the session is closed$"):
        await s.interrupt(triggered_by=HOST)
    with pytest.raises(TransportError, match="^session: the transport failed: line down$"):
        await asyncio.wait_for(asked, 1)
    await asyncio.wait_for(recorder, 1)
    assert not any(isinstance(e, Interrupted | Said) for e in seen)
    assert isinstance(seen[-1], Closed) and seen[-1].reason == "failed"


async def test_the_last_words_play_out_before_a_close_that_waits_on_quiet() -> None:
    link = SlowLoopback(ANA, "acme", delay=0.05)
    s = Session(link, speech=Tone())
    await s.start()
    seen, recorder = recording(s)
    asked = asyncio.create_task(s.say("Thank you. Goodbye.", triggered_by=HOST))
    await until(lambda: len(link.audio) > 0)
    await asyncio.wait_for(s.quiet(), 2)
    await s.close()
    await recorder
    said = await asked
    assert said.heard == "Thank you. Goodbye." and not said.interrupted
    assert [type(e) for e in seen] == [Speaking, Silent, Said, Closed]
    assert link.controls[-1]["event"]["kind"] == "closed"


class HungSend(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.sending = asyncio.Event()
        self.released = asyncio.Event()

    async def say(self, text: str) -> str | None:
        self.sending.set()
        while not self.released.is_set():
            with contextlib.suppress(asyncio.CancelledError):
                await self.released.wait()
        return None


class HangingSend(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.sending = asyncio.Event()

    async def say(self, text: str) -> str | None:
        self.sending.set()
        await asyncio.sleep(30)
        return None


async def test_a_close_returns_a_say_a_barge_in_ended_while_its_send_ran() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = connection = HangingSend()
    s = Session(link, live=live)
    await s.start()
    asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await asyncio.wait_for(connection.sending.wait(), 1)
    connection.emit(LiveInterrupted())
    await until(lambda: any(event("said")(c) for c in link.controls))
    await s.close()
    said = await asyncio.wait_for(asked, 1)
    assert said.interrupted and said.heard == ""


async def test_a_send_that_ignores_being_cancelled_strands_no_say(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("rfnry_senses.session.CLOSE_GRACE", 0.05)
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = connection = HungSend()
    s = Session(link, live=live)
    await s.start()
    asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await asyncio.wait_for(connection.sending.wait(), 1)
    connection.emit(LiveInterrupted())
    await until(lambda: any(event("said")(c) for c in link.controls))
    await asyncio.wait_for(s.close(), 1)
    said = await asyncio.wait_for(asked, 1)
    connection.released.set()
    assert said.interrupted and said.heard == ""


async def test_a_silent_response_overtaken_as_the_model_fails_still_ends_its_say() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = Session(link, live=live)
    await s.start()
    asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await until(lambda: len(live.connection.calls) == 1)
    live.connection.emit(
        LiveStarted(requested=True), LiveDone(), LiveStarted(), LiveFailed(message="gone")
    )
    said = await asyncio.wait_for(asked, 1)
    assert said.heard == "" and not said.interrupted

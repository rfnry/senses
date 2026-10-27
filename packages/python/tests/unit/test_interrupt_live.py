import asyncio
from typing import Any

import pytest

from rfnry_senses import session as session_module
from rfnry_senses.contracts import (
    LiveAudio,
    LiveDone,
    LiveHeard,
    LiveInterrupted,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
)
from rfnry_senses.events import Event, Heard, Interrupted, Muted, Said, Silent, Speaking, ToolCalled
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools
from rfnry_senses.transport import Loopback, Played

from .conftest import Clock
from .fakes import ANA, HOST, ScriptedConnection, ScriptedLive, next_of, pcm, response, until


async def test_the_person_cutting_in_truncates_to_what_they_heard() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(
            LiveStarted(),
            LiveSaid(text="Your table "),
            LiveAudio(frame=pcm(100, 24000)),
            LiveSaid(text="is ready."),
            LiveAudio(frame=pcm(100, 24000)),
            LiveInterrupted(),
        )
        cut = await next_of(events, Interrupted)
        said = await next_of(events, Said)
    assert cut.heard == "Your table " and cut.triggered_by == ANA
    assert said.interrupted and said.heard == "Your table " and said.triggered_by is None
    assert live.connection.interrupts == [(100, "Your table ", None)]
    assert {"type": "stop", "utterance": 1} in link.controls


async def test_audio_after_the_cut_never_reaches_the_person() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(
            LiveStarted(),
            LiveAudio(frame=pcm(100, 24000)),
            LiveInterrupted(),
            LiveAudio(frame=pcm(100, 24000)),
            LiveSaid(text="late"),
            LiveDone(),
            *response("Next."),
        )
        await next_of(events, Interrupted)
        nxt = await next_of(events, Said)
        nxt = nxt if nxt.text == "Next." else await next_of(events, Said)
    assert len(link.audio) == 2 and nxt.text == "Next." and not nxt.interrupted


async def test_a_silent_client_is_estimated_from_the_clock(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_module, "PLAYED_GRACE", 0.01)
    live, link = ScriptedLive(), Loopback(ANA, "acme", answer=False)
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(
            LiveStarted(),
            LiveSaid(text="One. "),
            LiveAudio(frame=pcm(100, 24000)),
            LiveSaid(text="Two. "),
            LiveAudio(frame=pcm(100, 24000)),
        )
        await until(lambda: len(link.audio) == 2)
        clock.advance(0.15)
        live.connection.emit(LiveInterrupted())
        cut = await next_of(events, Interrupted)
    assert cut.heard == "One. " and live.connection.interrupts == [(150, "One. ", None)]


async def test_a_silent_client_at_the_natural_end_heard_everything(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_module, "PLAYBACK_SLACK", 0.01)
    live, link = ScriptedLive(), Loopback(ANA, "acme", answer=False)
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(*response("One. ", "Two. "))
        await until(lambda: len(link.audio) == 2)
        clock.advance(10)
        said = await next_of(events, Said)
    assert said.heard == "One. Two. " and not said.interrupted


async def test_a_tool_call_made_by_an_interrupted_response_is_answered_once() -> None:
    gate = asyncio.Event()

    async def terrace(args: dict[str, Any]) -> dict[str, bool]:
        await gate.wait()
        return {"open": True}

    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    s = Session(link, live=live)
    await s.start(tools=Tools([Tool("terrace", "Open?", body=terrace)]))
    async with s:
        events = s.events()
        live.connection.emit(
            LiveStarted(),
            LiveSaid(text="Let me check. "),
            LiveAudio(frame=pcm(100, 24000)),
            LiveToolCall(call_id="c1", name="terrace"),
            LiveInterrupted(),
        )
        cut = await next_of(events, Interrupted)
        gate.set()
        await next_of(events, ToolCalled)
        live.connection.emit(LiveDone())
        await asyncio.sleep(0.01)
    assert live.connection.results == [("c1", '{"open": true}', False)]
    assert cut.heard == "Let me check. " and live.connection.interrupts == [
        (100, "Let me check. ", None)
    ]


async def test_no_result_is_submitted_after_close() -> None:
    async def forever(args: dict[str, Any]) -> str:
        await asyncio.Event().wait()
        return "never"

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = Session(link, live=live, tool_timeout=None)
    await s.start(tools=Tools([Tool("forever", "Never", body=forever)]))
    async with s:
        live.connection.emit(LiveToolCall(call_id="c1", name="forever"))
        await asyncio.sleep(0.01)
    assert live.connection.results == []


async def test_interrupt_by_code_and_the_refusing_vendor() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 0)
    live.connection.refuse_interrupt = True
    async with Session(link, live=live) as s:
        assert await s.interrupt(triggered_by=HOST) is None
        live.connection.emit(
            LiveStarted(), LiveSaid(text="Hello. "), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 1)
        cut = await s.interrupt(triggered_by=HOST)
    assert cut is not None and cut.heard == "" and cut.triggered_by == HOST


async def test_muting_cuts_the_voice_and_gates_what_follows() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(
            LiveStarted(), LiveSaid(text="Hello. "), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 1)
        await s.mute(triggered_by=HOST)
        muted = await next_of(events, Muted)
        cut = await next_of(events, Interrupted)
        cut_said = await next_of(events, Said)
        live.connection.emit(LiveDone(), *response("Unheard."))
        await until(lambda: live.connection.interrupts == [(100, "Hello. ", None), (0, "", None)])
        gated = await s.say("Hello?", triggered_by=HOST)
        await s.unmute(triggered_by=HOST)
        unmuted = await next_of(events, Muted)
    assert muted.muted and muted.triggered_by == HOST and cut.triggered_by == HOST
    assert cut_said.heard == "Hello. " and cut_said.interrupted
    assert (
        gated.heard == "" and gated.interrupted and ("say", "Hello?") not in live.connection.calls
    )
    assert not unmuted.muted and len(link.audio) == 1


async def test_the_person_cutting_in_ends_the_queued_says_and_a_late_answer_has_no_cause() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    async with Session(link, live=live) as s:
        events = s.events()
        first = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        second = asyncio.create_task(s.say("Shall I order the usual?", triggered_by=HOST))
        third = asyncio.create_task(s.prompt("Offer the terrace.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 3)
        live.connection.emit(
            LiveStarted(),
            LiveSaid(text="Your table "),
            LiveAudio(frame=pcm(100, 24000)),
            LiveInterrupted(),
            LiveDone(),
        )
        said_first = await first
        queued = await asyncio.wait_for(asyncio.gather(second, third), 1)
        live.connection.emit(*response("Sure, what would you like?"))
        saids = [await next_of(events, Said) for _ in range(4)]
    assert said_first.heard == "Your table " and said_first.interrupted
    assert all(q.heard == "" and q.interrupted and q.triggered_by == HOST for q in queued)
    assert saids[3].text == "Sure, what would you like?" and saids[3].triggered_by is None


class Unanswered(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.answer = asyncio.Event()
        self.refusal: Exception | None = None

    async def say(self, text: str) -> None:
        await super().say(text)
        if len(self.calls) > 1:
            await self.answer.wait()
            if self.refusal is not None:
                raise self.refusal


async def cut_in_while_a_say_is_unanswered(
    s: Session, connection: Unanswered
) -> tuple[asyncio.Task[Said], asyncio.Task[Said]]:
    first = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
    await until(lambda: len(connection.calls) == 1)
    unanswered = asyncio.create_task(s.say("Shall I order the usual?", triggered_by=HOST))
    await until(lambda: len(connection.calls) == 2)
    connection.emit(
        LiveStarted(),
        LiveSaid(text="Your table "),
        LiveAudio(frame=pcm(100, 24000)),
        LiveInterrupted(),
        LiveHeard(text="wait", final=True),
    )
    return first, unanswered


async def test_a_barge_in_does_not_wait_on_a_say_the_vendor_has_not_answered() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    live.connection = connection = Unanswered()
    async with Session(link, live=live) as s:
        events = s.events()
        first, unanswered = await cut_in_while_a_say_is_unanswered(s, connection)
        heard = await next_of(events, Heard)
        said_first = await first
        connection.answer.set()
        said_unanswered = await unanswered
    assert heard.text == "wait" and said_first.heard == "Your table " and said_first.interrupted
    assert said_unanswered.heard == "" and said_unanswered.interrupted


async def test_a_say_the_vendor_refuses_after_a_barge_in_raises_the_vendors_error() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    live.connection = connection = Unanswered()
    async with Session(link, live=live) as s:
        events = s.events()
        _, unanswered = await cut_in_while_a_say_is_unanswered(s, connection)
        await next_of(events, Heard)
        connection.refusal = RuntimeError("the vendor refused")
        connection.answer.set()
        with pytest.raises(RuntimeError, match="^the vendor refused$"):
            await unanswered


class Client(Loopback):
    def __init__(self, stops: dict[int, int] | None = None) -> None:
        super().__init__(ANA, "acme", answer=False)
        self.stops = stops or {}

    def report(self, utterance: int, ms: int) -> None:
        self._inbox.put_nowait(Played(utterance=utterance, ms=ms))

    async def send_control(self, control: dict[str, Any]) -> None:
        await super().send_control(control)
        if control["type"] == "stop":
            for number, ms in self.stops.items():
                if number <= control["utterance"]:
                    self.report(number, ms)


def voice(events: list[Event]) -> list[str]:
    return [
        f"{type(e).__name__}:{e.heard}" if isinstance(e, Said | Interrupted) else type(e).__name__
        for e in events
        if isinstance(e, Speaking | Silent | Said | Interrupted)
    ]


async def collecting(s: Session) -> tuple[list[Event], asyncio.Task[None]]:
    seen: list[Event] = []
    events = s.events()

    async def collect() -> None:
        async for event in events:
            seen.append(event)

    return seen, asyncio.create_task(collect())


async def test_an_overtaken_utterance_is_what_the_client_reported_of_it() -> None:
    live, link = ScriptedLive(), Client()
    async with Session(link, live=live) as s:
        seen, collector = await collecting(s)
        live.connection.emit(*response("Let me ", "check that. "))
        await until(lambda: {"type": "silent", "utterance": 1} in link.controls)
        live.connection.emit(*response("It is open. "))
        await until(lambda: {"type": "silent", "utterance": 2} in link.controls)
        await asyncio.sleep(0.01)
        assert voice(seen) == ["Speaking"]
        link.report(1, 100)
        link.report(2, 100)
        await until(lambda: len(voice(seen)) == 4)
    await collector
    assert voice(seen) == ["Speaking", "Said:Let me ", "Silent", "Said:It is open. "]


async def test_a_cut_in_the_older_tail_keeps_what_was_heard_and_nothing_of_the_newer() -> None:
    live, link = ScriptedLive(), Client(stops={1: 100, 2: 0})
    async with Session(link, live=live) as s:
        seen, collector = await collecting(s)
        live.connection.emit(*response("Let me ", "check that. "))
        await until(lambda: {"type": "silent", "utterance": 1} in link.controls)
        live.connection.emit(
            LiveStarted(), LiveSaid(text="It is open. "), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 3)
        live.connection.emit(LiveInterrupted())
        await until(lambda: len(voice(seen)) == 5)
    await collector
    assert voice(seen) == [
        "Speaking",
        "Interrupted:Let me ",
        "Said:Let me ",
        "Silent",
        "Said:",
    ]
    saids = [e for e in seen if isinstance(e, Said)]
    assert all(said.interrupted for said in saids) and saids[1].text == "It is open. "
    assert live.connection.interrupts == [(100, "Let me ", None)]
    assert [c for c in link.controls if c["type"] == "stop"] == [{"type": "stop", "utterance": 2}]


async def test_back_to_back_utterances_are_one_stretch_of_voice() -> None:
    live, link = ScriptedLive(), Client()
    async with Session(link, live=live) as s:
        seen, collector = await collecting(s)
        live.connection.emit(*response("One. "))
        await until(lambda: {"type": "silent", "utterance": 1} in link.controls)
        live.connection.emit(*response("Two. "))
        await until(lambda: {"type": "silent", "utterance": 2} in link.controls)
        link.report(1, 100)
        await until(lambda: len(voice(seen)) == 2)
        link.report(2, 100)
        await until(lambda: len(voice(seen)) == 4)
    await collector
    assert voice(seen) == ["Speaking", "Said:One. ", "Silent", "Said:Two. "]
    assert [c["type"] for c in link.controls if c["type"] in ("speaking", "silent")] == [
        "speaking",
        "silent",
        "speaking",
        "silent",
    ]


async def test_a_silent_client_is_estimated_from_where_each_utterance_began_to_play(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(session_module, "PLAYED_GRACE", 0.01)
    live, link = ScriptedLive(), Client()
    async with Session(link, live=live) as s:
        seen, collector = await collecting(s)
        live.connection.emit(*response("Let me ", "check that. "))
        await until(lambda: {"type": "silent", "utterance": 1} in link.controls)
        live.connection.emit(
            LiveStarted(), LiveSaid(text="It is open. "), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 3)
        clock.advance(0.15)
        live.connection.emit(LiveInterrupted())
        await until(lambda: len(voice(seen)) == 5)
    await collector
    assert voice(seen) == [
        "Speaking",
        "Interrupted:Let me ",
        "Said:Let me ",
        "Silent",
        "Said:",
    ]
    assert live.connection.interrupts == [(150, "Let me ", None)]


async def test_a_cut_after_a_silent_response_still_reaches_the_model() -> None:
    live, link = ScriptedLive(), Client(stops={1: 100})
    async with Session(link, live=live) as s:
        seen, collector = await collecting(s)
        live.connection.emit(*response("Let me ", "check that. "))
        await until(lambda: {"type": "silent", "utterance": 1} in link.controls)
        live.connection.emit(LiveStarted(), LiveSaid(text="Checking."), LiveDone())
        await until(lambda: any(isinstance(e, Said) and e.text == "Checking." for e in seen))
        live.connection.emit(LiveInterrupted())
        cut = await next_of(s.events(), Interrupted)
    await collector
    assert cut.heard == "Let me " and live.connection.interrupts == [(100, "Let me ", None)]


async def test_the_cut_names_the_response_it_landed_in() -> None:
    live, link = ScriptedLive(), Client(stops={1: 100, 2: 0})
    async with Session(link, live=live) as s:
        events = s.events()
        live.connection.emit(
            LiveStarted(id="r1"),
            LiveSaid(text="Let me "),
            LiveAudio(frame=pcm(100, 24000)),
            LiveSaid(text="check that. "),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        await until(lambda: {"type": "silent", "utterance": 1} in link.controls)
        live.connection.emit(
            LiveStarted(id="r2"), LiveSaid(text="It is open. "), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 3)
        live.connection.emit(LiveInterrupted())
        await next_of(events, Interrupted)
    assert live.connection.interrupts == [(100, "Let me ", "r1")]

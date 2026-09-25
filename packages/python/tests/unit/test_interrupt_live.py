import asyncio
from typing import Any

import pytest

from rfnry_senses import session as session_module
from rfnry_senses.contracts import (
    LiveAudio,
    LiveDone,
    LiveInterrupted,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
)
from rfnry_senses.events import Interrupted, Muted, Said, ToolCalled
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools
from rfnry_senses.transport import Loopback

from .conftest import Clock
from .fakes import ANA, HOST, ScriptedLive, next_of, pcm, response, until


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
    assert live.connection.interrupts == [100]
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
    assert cut.heard == "One. " and live.connection.interrupts == [150]


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
    assert cut.heard == "Let me check. " and live.connection.interrupts == [100]


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
        await until(lambda: live.connection.interrupts == [100, 0])
        gated = await s.say("Hello?", triggered_by=HOST)
        await s.unmute(triggered_by=HOST)
        unmuted = await next_of(events, Muted)
    assert muted.muted and muted.triggered_by == HOST and cut.triggered_by == HOST
    assert cut_said.heard == "Hello. " and cut_said.interrupted
    assert (
        gated.heard == "" and gated.interrupted and ("say", "Hello?") not in live.connection.calls
    )
    assert not unmuted.muted and len(link.audio) == 1

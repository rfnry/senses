import asyncio
from typing import Any

import pytest

from rfnry_senses.contracts import (
    AudioFrame,
    LiveAudio,
    LiveDone,
    LiveInterrupted,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
)
from rfnry_senses.errors import ConfigurationError, ModelError
from rfnry_senses.events import Closed, Informed, Interrupted, Said, Silent, Speaking
from rfnry_senses.senses_tools import SensesTools
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools
from rfnry_senses.transport import Loopback, Played

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


async def test_a_duplex_inform_goes_during_a_cut() -> None:
    link, live = Loopback(ANA, "acme", answer=False), DuplexLive()
    async with Session(link, live=live) as s:
        await speaking(link, live)
        cutting = asyncio.create_task(s.interrupt(triggered_by=HOST))
        await until(lambda: stopped(link))
        await asyncio.wait_for(s.inform("the news", triggered_by=HOST), 0.1)
        assert not cutting.done()
        await cutting
    assert live.connection.calls == [("text", "the news", False)]


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


def tone(ms: int, level: int) -> AudioFrame:
    return AudioFrame(data=level.to_bytes(2, "little", signed=True) * (16 * ms), sample_rate=16000)


def voice(ms: int) -> AudioFrame:
    return tone(ms, 1000)


async def silence(live: DuplexLive, frame: AudioFrame | None = None) -> None:
    while True:
        live.connection.emit(LiveAudio(frame=frame or pcm(20)))
        await asyncio.sleep(0.02)


def duplex(link: Loopback, live: DuplexLive, **kwargs: Any) -> Session:
    return Session(link, live=live, voice_gap=0.05, **kwargs)


def edges(link: Loopback, kind: str) -> list[int]:
    return [control["utterance"] for control in link.controls if control["type"] == kind]


def consulting(answers: list[dict[str, Any]]) -> Tools:
    async def consult(args: dict[str, Any]) -> str:
        answers.append(args)
        return "the terrace is open"

    return Tools([Tool("consult", "Ask the other mind.", body=consult)])


async def test_audio_with_no_response_reaches_the_person() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    frame = voice(20)
    async with duplex(link, live):
        live.connection.emit(LiveAudio(frame=frame))
        await until(lambda: bool(edges(link, "silent")))
    assert link.audio == [frame]
    assert [c["type"] for c in link.controls if c["type"] in ("speaking", "silent")] == [
        "speaking",
        "silent",
    ]


async def test_a_turn_based_model_still_drops_audio_with_no_response() -> None:
    link, live = Loopback(ANA, "acme"), ScriptedLive()
    async with Session(link, live=live, voice_gap=0.05):
        live.connection.emit(LiveAudio(frame=voice(20)), LiveSaid(text="hi "))
        await asyncio.sleep(0.1)
    assert link.audio == [] and edges(link, "speaking") == []


async def test_pure_silence_starts_no_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    s = duplex(link, live)
    await s.start()
    events = s.events()
    before = list(link.controls)
    live.connection.emit(*[LiveAudio(frame=pcm(20)) for _ in range(5)])
    await asyncio.sleep(0.1)
    assert link.audio == [] and link.controls == before
    await s.close()
    assert [type(e) async for e in events] == [Closed]


async def test_silence_inside_a_stretch_reaches_the_person() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    pause = pcm(20)
    async with duplex(link, live):
        live.connection.emit(
            LiveAudio(frame=voice(20)), LiveAudio(frame=pause), LiveAudio(frame=voice(20))
        )
        await until(lambda: bool(edges(link, "silent")))
    assert len(link.audio) == 3 and link.audio[1] == pause
    assert edges(link, "speaking") == [1]


async def test_a_continuous_stream_ends_its_stretch_when_the_voice_stops() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="hello "), LiveAudio(frame=voice(20)))
        stream = asyncio.create_task(silence(live))
        said = await next_of(events, Said)
        await asyncio.wait_for(s.quiet(), 1)
        assert not stream.done() and edges(link, "silent") == [1]
        sent = len(link.audio)
        await asyncio.sleep(0.06)
        stream.cancel()
    assert said.text == "hello " and said.heard == "hello "
    assert len(link.audio) == sent and edges(link, "speaking") == [1]


async def test_voice_after_silence_longer_than_the_gap_starts_a_new_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        stream = asyncio.create_task(silence(live))
        live.connection.emit(LiveSaid(text="one "), LiveAudio(frame=voice(20)))
        first = await next_of(events, Said)
        await asyncio.sleep(0.1)
        live.connection.emit(LiveSaid(text="two "), LiveAudio(frame=voice(20)))
        second = await next_of(events, Said)
        stream.cancel()
    assert [first.text, second.text] == ["one ", "two "]
    assert edges(link, "speaking") == [1, 2]


async def test_words_with_only_silence_after_them_are_not_voice() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        stream = asyncio.create_task(silence(live))
        live.connection.emit(LiveSaid(text="hi "))
        said = await next_of(events, Said)
        stream.cancel()
    assert said.text == "hi " and said.heard == ""
    assert link.audio == [] and edges(link, "speaking") == []


async def test_a_stretch_reports_as_a_response_does() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="hello "), LiveAudio(frame=voice(20)))
        async with asyncio.timeout(1):
            told = [await anext(events) for _ in range(3)]
    assert [type(e) for e in told] == [Speaking, Silent, Said]
    said = told[2]
    assert isinstance(said, Said)
    assert said.text == "hello " and said.heard == "hello "
    assert not said.interrupted and said.triggered_by is None


async def test_audio_sent_ahead_stays_one_stretch_until_it_has_played() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live):
        live.connection.emit(*[LiveAudio(frame=voice(20)) for _ in range(15)])
        await asyncio.sleep(0.25)
        assert edges(link, "silent") == []
        await until(lambda: bool(edges(link, "silent")))
    assert len(link.audio) == 15 and edges(link, "speaking") == [1]


async def test_a_gap_in_arrivals_while_audio_still_plays_is_one_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live):
        live.connection.emit(LiveAudio(frame=voice(300)))
        await asyncio.sleep(0.15)
        live.connection.emit(LiveAudio(frame=voice(200)))
        await asyncio.sleep(0.3)
        assert edges(link, "silent") == []
        await until(lambda: bool(edges(link, "silent")))
    assert edges(link, "speaking") == [1]


async def test_a_pause_longer_than_the_gap_makes_two_stretches() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="one "), LiveAudio(frame=voice(20)))
        await asyncio.sleep(0.2)
        live.connection.emit(LiveSaid(text="two "), LiveAudio(frame=voice(20)))
        saids = [await next_of(events, Said) for _ in range(2)]
    assert [said.text for said in saids] == ["one ", "two "]
    assert edges(link, "speaking") == [1, 2]


async def test_quiet_returns_once_a_model_that_never_marks_its_speech_is_silent() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        live.connection.emit(LiveAudio(frame=voice(20)))
        await until(lambda: bool(link.audio))
        await asyncio.wait_for(s.quiet(), 2)
        assert edges(link, "silent") == [1]


async def test_a_response_ends_the_open_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    s = duplex(link, live)
    await s.start()
    events = s.events()
    live.connection.emit(LiveAudio(frame=voice(20)))
    await until(lambda: bool(link.audio))
    live.connection.emit(LiveStarted(requested=False), LiveAudio(frame=voice(20)), LiveDone())
    await asyncio.wait_for(s.quiet(), 1)
    await s.close()
    told = [e async for e in events]
    assert len([e for e in told if isinstance(e, Said)]) == 2
    assert isinstance(told[-1], Closed) and told[-1].reason == "closed"
    assert edges(link, "speaking") == [1, 2]


async def test_a_done_ends_the_open_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    s = duplex(link, live)
    await s.start()
    events = s.events()
    live.connection.emit(LiveAudio(frame=voice(20)), LiveDone())
    await asyncio.wait_for(s.quiet(), 1)
    await asyncio.sleep(0.1)
    await s.close()
    told = [e async for e in events]
    assert [type(e) for e in told] == [Speaking, Silent, Said, Closed]
    assert isinstance(told[-1], Closed) and told[-1].reason == "closed"


async def test_a_done_after_the_stretch_went_quiet_is_ignored() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    s = duplex(link, live)
    await s.start()
    events = s.events()
    live.connection.emit(LiveAudio(frame=voice(20)))
    await until(lambda: bool(edges(link, "silent")))
    live.connection.emit(LiveDone(), LiveAudio(frame=voice(20)))
    await until(lambda: len(link.audio) == 2)
    await asyncio.wait_for(s.quiet(), 1)
    await s.close()
    told = [e async for e in events]
    assert isinstance(told[-1], Closed) and told[-1].reason == "closed"
    assert edges(link, "speaking") == [1, 2]


async def test_a_say_during_a_stretch_gets_its_own_said() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="so "), LiveAudio(frame=voice(300)))
        await until(lambda: bool(link.audio))
        saying = asyncio.create_task(s.say("Exactly this.", triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(
            LiveStarted(requested="request-1"),
            LiveSaid(text="Exactly this."),
            LiveAudio(frame=voice(20)),
            LiveDone(),
        )
        said = await asyncio.wait_for(saying, 1)
        stretch = await next_of(events, Said)
    assert said.heard == said.text == "Exactly this." and said.triggered_by == HOST
    assert stretch.text == "so " and stretch.heard == "so " and stretch.triggered_by is None


async def test_a_tag_claims_its_prompt_from_an_open_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    live.connection.tagging = True
    async with duplex(link, live) as s:
        prompting = asyncio.create_task(s.prompt("the news", triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(LiveAudio(frame=voice(300)))
        await until(lambda: bool(link.audio))
        assert not prompting.done()
        live.connection.emit(
            LiveStarted(requested="request-1"),
            LiveSaid(text="Here it is. "),
            LiveAudio(frame=voice(20)),
            LiveDone(),
        )
        said = await asyncio.wait_for(prompting, 1)
    assert said is not None
    assert said.text == "Here it is. " and said.triggered_by == HOST


async def test_audio_after_a_response_starts_a_new_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(
            LiveStarted(requested=False),
            LiveSaid(text="one "),
            LiveAudio(frame=voice(20)),
            LiveDone(),
            LiveAudio(frame=voice(20)),
            LiveSaid(text="two "),
        )
        saids = [await next_of(events, Said) for _ in range(2)]
    assert edges(link, "speaking") == [1, 2]
    assert [said.heard for said in saids] == ["one ", "two "]
    assert saids[1].triggered_by is None


async def test_a_cut_ends_the_stretch_and_later_audio_starts_a_new_one() -> None:
    link, live = Loopback(ANA, "acme", played=lambda utterance, sent: 100), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(
            LiveSaid(text="one "),
            LiveAudio(frame=voice(100)),
            LiveSaid(text="two "),
            LiveAudio(frame=voice(100)),
        )
        await until(lambda: len(link.audio) == 2)
        cut = await s.interrupt(triggered_by=HOST)
        said = await next_of(events, Said)
        live.connection.emit(LiveAudio(frame=voice(20)))
        await until(lambda: len(link.audio) == 3)
    assert cut is not None and cut.heard == "one "
    assert said.heard == "one " and said.interrupted
    assert live.connection.interrupts == [(100, "one ", None)]
    assert edges(link, "speaking") == [1, 2]


async def test_audio_during_a_cut_starts_no_stretch() -> None:
    link, live = Loopback(ANA, "acme", answer=False), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveAudio(frame=voice(200)))
        await until(lambda: bool(link.audio))
        cutting = asyncio.create_task(s.interrupt(triggered_by=HOST))
        await until(lambda: bool(edges(link, "stop")))
        live.connection.emit(LiveSaid(text="still going "), LiveAudio(frame=voice(20)))
        await next_of(events, Interrupted)
        await cutting
        assert len(link.audio) == 1 and edges(link, "speaking") == [1]
        live.connection.emit(LiveAudio(frame=voice(20)))
        await until(lambda: len(link.audio) == 2)
    assert edges(link, "speaking") == [1, 2]


async def test_words_of_a_cut_response_start_no_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    s = duplex(link, live)
    await s.start()
    events = s.events()
    live.connection.emit(LiveStarted(requested=False), LiveAudio(frame=voice(100)))
    await until(lambda: bool(link.audio))
    await s.interrupt(triggered_by=HOST)
    live.connection.emit(LiveSaid(text="tail "), LiveDone())
    await asyncio.wait_for(s.quiet(), 1)
    await asyncio.sleep(0.1)
    await s.close()
    told = [e async for e in events]
    assert len([e for e in told if isinstance(e, Said)]) == 1
    assert isinstance(told[-1], Closed) and told[-1].reason == "closed"


async def test_a_live_interrupted_cuts_a_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="one "), LiveAudio(frame=voice(200)))
        await until(lambda: bool(link.audio))
        live.connection.emit(LiveInterrupted())
        cut = await next_of(events, Interrupted)
        live.connection.emit(LiveAudio(frame=voice(20)))
        await until(lambda: len(link.audio) == 2)
    assert cut.triggered_by == ANA
    assert len(live.connection.interrupts) == 1
    assert edges(link, "speaking") == [1, 2]


async def test_muted_audio_starts_no_stretch() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    s = duplex(link, live)
    await s.start()
    await s.mute(triggered_by=HOST)
    events = s.events()
    live.connection.emit(LiveAudio(frame=voice(20)), LiveSaid(text="hi "))
    await asyncio.sleep(0.1)
    await s.close()
    assert link.audio == []
    assert [type(e) async for e in events] == [Closed]


async def test_a_mute_cuts_a_stretch_and_unmuting_lets_audio_start_one() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        live.connection.emit(LiveAudio(frame=voice(200)))
        await until(lambda: bool(link.audio))
        await s.mute(triggered_by=HOST)
        live.connection.emit(LiveAudio(frame=voice(20)))
        await asyncio.sleep(0.02)
        await s.unmute(triggered_by=HOST)
        live.connection.emit(LiveAudio(frame=voice(20)))
        await until(lambda: len(link.audio) == 2)
    assert edges(link, "speaking") == [1, 2]


async def test_words_after_a_stretch_went_quiet_join_it() -> None:
    link, live = Loopback(ANA, "acme", answer=False), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="hello "), LiveAudio(frame=voice(20)))
        await until(lambda: bool(edges(link, "silent")))
        live.connection.emit(LiveSaid(text="tail "))
        await asyncio.sleep(0.01)
        await link._inbox.put(Played(utterance=1, ms=20))
        said = await next_of(events, Said)
    assert said.text == "hello tail " and said.heard == "hello tail "


@pytest.mark.parametrize(("played", "heard"), [(20, "hello tail "), (10, "")])
async def test_late_words_land_at_the_end_of_the_stretch_they_join(played: int, heard: str) -> None:
    link, live = Loopback(ANA, "acme", answer=False), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="hello "), LiveAudio(frame=voice(20)))
        await until(lambda: bool(edges(link, "silent")))
        live.connection.emit(LiveSaid(text="tail "))
        await asyncio.sleep(0.01)
        cutting = asyncio.create_task(s.interrupt(triggered_by=HOST))
        await until(lambda: bool(edges(link, "stop")))
        await link._inbox.put(Played(utterance=1, ms=played))
        await cutting
        said = await next_of(events, Said)
    assert said.heard == heard and said.interrupted


async def test_words_with_no_stretch_open_start_one() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="hi "))
        await asyncio.sleep(0.01)
        live.connection.emit(LiveAudio(frame=voice(20)))
        said = await next_of(events, Said)
    assert said.text == "hi " and said.heard == "hi "
    assert edges(link, "speaking") == [1]


async def test_words_that_never_get_a_voice_are_not_heard() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="hi "))
        said = await next_of(events, Said)
    assert said.text == "hi " and said.heard == "" and not said.interrupted
    assert link.audio == []


async def test_a_hand_off_during_a_stretch_is_answered_once() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    answers: list[dict[str, Any]] = []
    s = duplex(link, live)
    await s.start(tools=consulting(answers))
    async with s:
        live.connection.emit(
            LiveAudio(frame=voice(100)),
            LiveToolCall(call_id="c1", name="consult", args={"conversation": "..."}),
        )
        await until(lambda: bool(live.connection.results))
        await asyncio.wait_for(s.quiet(), 1)
    assert answers == [{"conversation": "..."}]
    assert live.connection.results == [("c1", "the terrace is open", False, True)]


async def test_a_hand_off_answered_silently() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    s = duplex(link, live)
    await s.start(tools=consulting([]), inform=["consult"])
    async with s:
        live.connection.emit(
            LiveAudio(frame=voice(100)),
            LiveToolCall(call_id="c1", name="consult", args={"conversation": "..."}),
        )
        await until(lambda: bool(live.connection.results))
    assert live.connection.results == [("c1", "the terrace is open", False, False)]


async def test_a_session_closing_during_a_stretch_ends_cleanly() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    s = duplex(link, live)
    await s.start()
    events = s.events()
    live.connection.emit(LiveSaid(text="hello "), LiveAudio(frame=voice(1000)))
    await until(lambda: bool(link.audio))
    quiet = asyncio.create_task(s.quiet())
    await asyncio.sleep(0.01)
    assert not quiet.done()
    await s.close()
    await asyncio.wait_for(quiet, 1)
    told = [e async for e in events]
    assert [type(e) for e in told] == [Speaking, Closed]
    assert isinstance(told[-1], Closed) and told[-1].reason == "closed"


async def test_a_turn_based_session_closing_during_a_response_reports_no_said() -> None:
    link, live = Loopback(ANA, "acme"), ScriptedLive()
    s = Session(link, live=live)
    await s.start()
    events = s.events()
    live.connection.emit(LiveStarted(), LiveSaid(text="hello "), LiveAudio(frame=voice(100)))
    await until(lambda: bool(link.audio))
    await s.close()
    assert [type(e) async for e in events] == [Speaking, Closed]


@pytest.mark.parametrize("gap", [0, -1])
def test_voice_gap_must_be_positive(gap: float) -> None:
    with pytest.raises(ConfigurationError, match="^session: voice_gap must be positive$"):
        Session(Loopback(ANA, "acme"), live=DuplexLive(), voice_gap=gap)


@pytest.mark.parametrize("level", [3, 69, 128, -128])
async def test_faint_noise_starts_no_stretch(level: int) -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    s = duplex(link, live)
    await s.start()
    events = s.events()
    before = list(link.controls)
    live.connection.emit(*[LiveAudio(frame=tone(20, level)) for _ in range(5)])
    await asyncio.sleep(0.1)
    assert link.audio == [] and link.controls == before
    await s.close()
    assert [type(e) async for e in events] == [Closed]


async def test_faint_noise_does_not_hold_a_stretch_open() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live) as s:
        events = s.events()
        live.connection.emit(LiveSaid(text="hello "), LiveAudio(frame=voice(20)))
        stream = asyncio.create_task(silence(live, tone(20, 3)))
        said = await asyncio.wait_for(next_of(events, Said), 1)
        await asyncio.wait_for(s.quiet(), 1)
        stream.cancel()
    assert said.text == "hello " and said.heard == "hello "
    assert edges(link, "speaking") == [1] and edges(link, "silent") == [1]


async def test_faint_noise_inside_a_stretch_reaches_the_person() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    pause = tone(20, 40)
    async with duplex(link, live):
        live.connection.emit(
            LiveAudio(frame=voice(20)), LiveAudio(frame=pause), LiveAudio(frame=voice(20))
        )
        await until(lambda: bool(edges(link, "silent")))
    assert len(link.audio) == 3 and link.audio[1] == pause
    assert edges(link, "speaking") == [1]


async def test_a_negative_peak_counts_as_voice() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live):
        live.connection.emit(LiveAudio(frame=tone(20, -129)))
        await until(lambda: bool(edges(link, "silent")))
    assert edges(link, "speaking") == [1]


async def test_voice_floor_can_be_raised_for_a_noisy_model() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live, voice_floor=2000):
        live.connection.emit(LiveAudio(frame=voice(20)))
        await asyncio.sleep(0.1)
        assert link.audio == []
        live.connection.emit(LiveAudio(frame=tone(20, 3000)))
        await until(lambda: bool(edges(link, "silent")))
    assert edges(link, "speaking") == [1]


async def test_a_floor_of_zero_leaves_only_exact_zeros_as_silence() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live, voice_floor=0):
        live.connection.emit(LiveAudio(frame=pcm(20)))
        await asyncio.sleep(0.1)
        assert link.audio == []
        live.connection.emit(LiveAudio(frame=tone(20, 1)))
        await until(lambda: bool(edges(link, "silent")))
    assert edges(link, "speaking") == [1]


async def test_faint_audio_inside_a_response_is_sent() -> None:
    link, live = Loopback(ANA, "acme"), DuplexLive()
    async with duplex(link, live):
        live.connection.emit(LiveStarted(requested=False), LiveAudio(frame=tone(20, 3)), LiveDone())
        await until(lambda: bool(edges(link, "silent")))
    assert len(link.audio) == 1


async def test_a_turn_based_model_plays_faint_audio_as_it_always_did() -> None:
    link, live = Loopback(ANA, "acme"), ScriptedLive()
    async with Session(link, live=live):
        live.connection.emit(LiveStarted(requested=False), LiveAudio(frame=tone(20, 3)), LiveDone())
        await until(lambda: bool(edges(link, "silent")))
    assert len(link.audio) == 1


@pytest.mark.parametrize("floor", [-1, 32768])
def test_voice_floor_must_fit_a_sample(floor: int) -> None:
    with pytest.raises(
        ConfigurationError, match="^session: voice_floor must be between 0 and 32767$"
    ):
        Session(Loopback(ANA, "acme"), live=DuplexLive(), voice_floor=floor)

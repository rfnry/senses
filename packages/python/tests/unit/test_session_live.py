import asyncio
import gc
import weakref
from collections.abc import AsyncIterator
from typing import Any

import pytest

from rfnry_senses.contracts import (
    LiveAudio,
    LiveDone,
    LiveFailed,
    LiveHeard,
    LiveInterrupted,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
)
from rfnry_senses.errors import ConfigurationError, InputError, ModelError, TransportError
from rfnry_senses.events import Closed, Heard, Informed, Said, Silent, Speaking, ToolCalled
from rfnry_senses.models import Image, Text
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools
from rfnry_senses.transport import Loopback

from .fakes import (
    ANA,
    HOST,
    ScriptedLive,
    ScriptedTranscription,
    Tone,
    next_of,
    pcm,
    response,
    until,
)


def session(link: Loopback, live: ScriptedLive, **kwargs: Any) -> Session:
    return Session(link, live=live, **kwargs)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (
            {"live": ScriptedLive(), "speech": Tone()},
            "^session: a live model hears and speaks itself",
        ),
        (
            {
                "transcription": ScriptedTranscription(),
                "speech": Tone(),
                "window": 0,
            },
            "^session: window must be positive$",
        ),
        (
            {"speech": Tone(), "instructions": "Be brief."},
            "^session: instructions are for a live model$",
        ),
        (
            {"live": ScriptedLive(), "tool_timeout": 0},
            "^session: tool_timeout must be positive or None$",
        ),
    ],
)
def test_a_session_that_cannot_work_is_refused(kwargs: dict[str, Any], message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        Session(Loopback(ANA, "acme"), **kwargs)


def test_a_transport_with_no_scope_is_refused() -> None:
    with pytest.raises(InputError, match="^session: scope is empty$"):
        Session(Loopback(ANA, ""), transcription=ScriptedTranscription())


async def test_the_live_model_is_opened_with_the_tools_and_hears_the_person() -> None:
    async def now(args: dict[str, Any]) -> str:
        return "12:00"

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live, instructions="Be brief.")
    await s.start(tools=Tools([Tool("now", "The time", body=now)]))
    async with s:
        assert s.scope == "acme" and s.person == ANA
        await link.speak(pcm(20))
        await until(lambda: len(live.connection.audio) == 1)
    assert [spec.name for spec in live.opened["tools"]] == ["now"] and live.opened[
        "instructions"
    ] == "Be brief."
    assert link.controls[0] == {"type": "ready"} and live.connection.closed


async def test_the_person_is_heard_as_the_model_transcribes_them() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(LiveHeard(text=" "), LiveHeard(text="a table for two", final=True))
        heard = await next_of(events, Heard)
    assert (
        heard.text == "a table for two"
        and heard.final
        and heard.speaker == ANA
        and heard.scope == "acme"
    )


async def test_say_speaks_verbatim_and_returns_what_was_heard() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        saying = asyncio.create_task(s.say("For two, at eight.", triggered_by=HOST))
        await until(lambda: live.connection.calls == [("say", "For two, at eight.")])
        live.connection.emit(*response("For two, ", "at eight."))
        said = await saying
        kinds = [type(await anext(events)) for _ in range(3)]
    assert said.text == "For two, at eight." and said.heard == "For two, at eight."
    assert not said.interrupted and said.triggered_by == HOST
    assert kinds == [Speaking, Silent, Said] and len(link.audio) == 2
    assert [c for c in link.controls if c["type"] in ("speaking", "silent")] == [
        {"type": "speaking", "utterance": 1},
        {"type": "silent", "utterance": 1},
    ]


async def test_prompt_returns_the_models_own_words_and_inform_asks_for_no_reply() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        prompting = asyncio.create_task(s.prompt("Tell Ana her table is ready.", triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(*response("Ana, ", "your table is ready."))
        said = await prompting
        await s.inform("The terrace closes at nine.", triggered_by=HOST)
        informed = await next_of(events, Informed)
    assert said.text == "Ana, your table is ready." and said.triggered_by == HOST
    assert live.connection.calls == [
        ("text", "Tell Ana her table is ready.", True),
        ("text", "The terrace closes at nine.", False),
    ]
    assert informed.triggered_by == HOST
    assert not [
        c for c in link.controls if c["type"] == "event" and c["event"]["kind"] == "informed"
    ]


async def test_concurrent_say_and_prompt_keep_their_causes_in_order() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        first = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
        second = asyncio.create_task(s.prompt("Greet Ana.", triggered_by=ANA))
        await until(lambda: len(live.connection.calls) == 2)
        live.connection.emit(
            *response("Hello."), *response("Hi ", "Ana."), *response("By the way.")
        )
        said_first, said_second = await first, await second
        saids = [await next_of(events, Said) for _ in range(3)]
    assert live.connection.calls == [("say", "Hello."), ("text", "Greet Ana.", True)]
    assert said_first.triggered_by == HOST and said_second.triggered_by == ANA
    assert said_second.text == "Hi Ana."
    assert saids[2].text == "By the way." and saids[2].triggered_by is None


async def test_a_response_the_vendor_started_takes_no_cause() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        asked = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(
            LiveStarted(requested=False),
            LiveSaid(text="Sure. "),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        own = await next_of(events, Said)
        waiting = not asked.done()
        live.connection.emit(
            LiveStarted(requested=True),
            LiveSaid(text="Your table is ready."),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        said = await asked
    assert waiting and own.text == "Sure. " and own.triggered_by is None
    assert said.triggered_by == HOST and said.heard == "Your table is ready."


async def test_an_unknown_response_keeps_the_order() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(
            LiveStarted(requested=None), LiveAudio(frame=pcm(100, 24000)), LiveDone()
        )
        said = await asked
        live.connection.emit(LiveStarted(requested=True), LiveSaid(text="And?"), LiveDone())
        saids = [await next_of(events, Said) for _ in range(2)]
    assert said.triggered_by == HOST and saids[1].text == "And?"
    assert saids[1].triggered_by is None


async def test_the_models_tool_calls_are_dispatched_and_answered() -> None:
    gate = asyncio.Event()

    async def terrace(args: dict[str, Any]) -> dict[str, bool]:
        return {"open": True}

    async def photo(args: dict[str, Any]) -> list[Any]:
        return [Image(media_type="image/png", data=b"png"), Text(text="the terrace")]

    async def slow(args: dict[str, Any]) -> str:
        await gate.wait()
        return "late"

    tools = Tools(
        [
            Tool("terrace", "Open?", body=terrace),
            Tool("photo", "A photo", body=photo),
            Tool("slow", "Slow", body=slow),
        ]
    )
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live, tool_timeout=0.05)
    await s.start(tools=tools)
    async with s:
        events = s.events()
        live.connection.emit(
            LiveToolCall(call_id="c1", name="terrace"),
            LiveToolCall(call_id="c2", name="photo"),
            LiveToolCall(call_id="c3", name="ghost"),
            LiveToolCall(call_id="c4", name="slow"),
        )
        called = [await next_of(events, ToolCalled) for _ in range(4)]
    results = {c: (result, is_error) for c, result, is_error, _ in live.connection.results}
    assert all(respond for *_, respond in live.connection.results)
    assert results["c1"] == ('{"open": true}', False)
    assert results["c2"][0] == [
        Image(media_type="image/png", data=b"png"),
        Text(text="the terrace"),
    ]
    assert results["c3"] == ("InputError: tools: unknown tool 'ghost'", True)
    assert results["c4"] == ("timed out after 0.05s", True)
    assert {c.name for c in called} == {"terrace", "photo", "ghost", "slow"}
    assert not [
        c for c in link.controls if c["type"] == "event" and c["event"]["kind"] == "tool_called"
    ]


async def test_a_failing_model_closes_the_session_and_fails_its_waiters() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        prompting = asyncio.create_task(s.prompt("Hello?", triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(LiveFailed(message="connection lost"))
        closed = await next_of(events, Closed)
        with pytest.raises(ModelError, match="^session: the live model failed: connection lost$"):
            await prompting
    assert (
        closed.reason == "failed"
        and closed.message == "session: the live model failed: connection lost"
    )
    assert link.closed


async def test_a_response_that_ends_before_it_starts_breaks_the_contract() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(LiveDone())
        closed = await next_of(events, Closed)
    assert closed.message == "session: a response ended before it started"


async def test_hanging_up_closes_the_session_and_its_verbs() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        await link.hang_up()
        closed = await next_of(events, Closed)
        with pytest.raises(TransportError, match="^session: the session is closed$"):
            await s.say("Hello.", triggered_by=HOST)
        late = [event async for event in s.events()]
    assert closed.reason == "hung_up" and late == []


async def test_every_subscription_sees_every_event() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        one, two = s.events(), s.events()
        live.connection.emit(LiveHeard(text="hi", final=True))
        assert (await next_of(one, Heard)).text == (await next_of(two, Heard)).text == "hi"
        with pytest.raises(InputError, match="^session: text is empty$"):
            await s.say("  ", triggered_by=HOST)
        with pytest.raises(ConfigurationError, match="^session: look needs video=True$"):
            await s.look(triggered_by=HOST)


async def test_a_consumer_nobody_holds_lives_as_long_as_its_session() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    seen: list[Heard] = []

    async def converse(s: Session) -> None:
        async for event in s.events():
            if isinstance(event, Heard):
                seen.append(event)

    async with session(link, live) as s:
        consumer = weakref.ref(asyncio.create_task(converse(s)))
        await asyncio.sleep(0)
        gc.collect()
        live.connection.emit(LiveHeard(text="hi", final=True))
        await until(lambda: bool(seen))
        task = consumer()
        assert task is not None and not task.done()


async def test_a_consumer_that_breaks_or_is_cancelled_is_let_go() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")

    async def first(s: Session) -> None:
        async for _ in s.events():
            break

    async def forever(s: Session) -> None:
        async for _ in s.events():
            pass

    async with session(link, live) as s:
        breaking = asyncio.create_task(first(s))
        cancelled = asyncio.create_task(forever(s))
        await asyncio.sleep(0)
        queues = [weakref.ref(q) for q in s._subscribers]
        live.connection.emit(LiveHeard(text="hi", final=True))
        cancelled.cancel()
        await asyncio.wait({breaking, cancelled})
        del breaking, cancelled
        gc.collect()
        assert len(queues) == 2 and [q() for q in queues] == [None, None]
        assert not s._subscribers


async def test_a_dropped_subscription_is_let_go() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        kept, dropped = s.events(), s.events()
        queue = weakref.ref(dropped._queue)
        del dropped
        gc.collect()
        live.connection.emit(LiveHeard(text="hi", final=True))
        assert (await next_of(kept, Heard)).text == "hi"
        assert queue() is None


async def test_a_live_say_joins_a_stream_and_sends_it_once() -> None:
    async def reply() -> AsyncIterator[str]:
        for chunk in ("For two, ", "at eight."):
            yield chunk

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        saying = asyncio.create_task(s.say(reply(), triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        live.connection.emit(*response("For two, ", "at eight."))
        said = await saying
    assert live.connection.calls == [("say", "For two, at eight.")]
    assert said.text == said.heard == "For two, at eight." and said.triggered_by == HOST


async def test_a_tool_named_in_inform_answers_without_a_reply() -> None:
    async def end_call(args: dict[str, Any]) -> str:
        return "ended"

    async def terrace(args: dict[str, Any]) -> str:
        return "open"

    tools = Tools([Tool("end_call", "End", body=end_call), Tool("terrace", "Open?", body=terrace)])
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    await s.start(tools=tools, inform=["end_call"])
    async with s:
        events = s.events()
        live.connection.emit(
            LiveToolCall(call_id="c1", name="end_call"), LiveToolCall(call_id="c2", name="terrace")
        )
        await next_of(events, ToolCalled)
        await next_of(events, ToolCalled)
    assert sorted(live.connection.results) == [
        ("c1", "ended", False, False),
        ("c2", "open", False, True),
    ]


async def test_inform_must_name_a_tool() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    with pytest.raises(
        ConfigurationError, match="^session: inform names 'ghost', which is not one of the tools$"
    ):
        await s.start(tools=Tools(), inform=["ghost"])


async def test_start_refuses_tools_without_a_live_model_and_starts_once() -> None:
    cascaded = Session(Loopback(ANA, "acme"), transcription=ScriptedTranscription(), speech=Tone())
    camera = Session(Loopback(ANA, "acme"), video=True)
    for s in (cascaded, camera):
        with pytest.raises(
            ConfigurationError,
            match="^session: tools are for a live model; without one they belong to your own loop$",
        ):
            await s.start(tools=Tools())
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    with pytest.raises(TransportError, match="^session: start the session before using it$"):
        await s.say("Hello.", triggered_by=HOST)
    await s.start()
    with pytest.raises(ConfigurationError, match="^session: the session has already started$"):
        await s.start()
    async with s:
        assert live.opened["tools"] == []
    with pytest.raises(TransportError, match="^session: the session is closed$"):
        await s.start()


def trailing_words() -> list[Any]:
    return [
        LiveStarted(),
        LiveSaid(text="Good "),
        LiveAudio(frame=pcm(100, 24000)),
        LiveAudio(frame=pcm(100, 24000)),
        LiveSaid(text="morning."),
        LiveDone(),
    ]


async def test_words_sent_after_the_last_audio_count_when_all_of_it_played() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(*trailing_words())
        said = await next_of(events, Said)
    assert said.heard == "Good morning." and not said.interrupted


async def test_a_partial_report_still_cuts_by_time() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 100)
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(*trailing_words())
        said = await next_of(events, Said)
    assert said.heard == "Good "


async def test_a_cut_still_cuts_by_time_when_words_run_ahead_of_the_audio() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", played=lambda utterance, sent: 200)
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(*trailing_words()[:-1])
        await until(lambda: len(link.audio) == 2)
        live.connection.emit(LiveInterrupted())
        said = await next_of(events, Said)
    assert said.heard == "Good " and said.interrupted

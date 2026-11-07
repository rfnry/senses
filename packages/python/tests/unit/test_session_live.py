import asyncio
import gc
import weakref
from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from rfnry_senses.contracts import (
    AudioFrame,
    LiveAudio,
    LiveDone,
    LiveFailed,
    LiveHeard,
    LiveInterrupted,
    LiveSaid,
    LiveStarted,
    LiveToolCall,
    LiveToolCancelled,
)
from rfnry_senses.errors import ConfigurationError, InputError, ModelError, TransportError
from rfnry_senses.events import (
    Cancelled,
    Closed,
    Heard,
    Informed,
    Interrupted,
    Said,
    Silent,
    Speaking,
    ToolCalled,
)
from rfnry_senses.models import Image, Text
from rfnry_senses.session import Session
from rfnry_senses.tools import Tool, Tools
from rfnry_senses.transport import Loopback

from .fakes import (
    ANA,
    HOST,
    JPEG,
    AwaitingLoopback,
    ScriptedConnection,
    ScriptedLive,
    ScriptedTranscription,
    SlowLoopback,
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
    assert said is not None
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
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(*response("Hello."))
        await until(lambda: len(live.connection.calls) == 2)
        live.connection.emit(*response("Hi ", "Ana."), *response("By the way."))
        said_first, said_second = await first, await second
        saids = [await next_of(events, Said) for _ in range(3)]
    assert live.connection.calls == [("say", "Hello."), ("text", "Greet Ana.", True)]
    assert said_second is not None
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


async def test_a_tagged_late_answer_is_cut_and_the_next_request_keeps_its_own() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection.tagging = True
    async with session(link, live) as s:
        events = s.events()
        first = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 1)
        await s.mute(triggered_by=HOST)
        await s.unmute(triggered_by=HOST)
        ended = await first
        second = asyncio.create_task(s.prompt("Offer the terrace.", triggered_by=ANA))
        await until(lambda: len(live.connection.calls) == 2)
        live.connection.emit(
            LiveStarted(id="late", requested="request-1"),
            LiveSaid(text="Your table is ready."),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
            LiveStarted(id="own", requested="request-2"),
            LiveSaid(text="Would you like the terrace?"),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        said = await asyncio.wait_for(second, 1)
        saids = [await next_of(events, Said) for _ in range(2)]
    assert ended.heard == "" and ended.interrupted
    assert live.connection.interrupts == [(0, "", "late")]
    assert said is not None
    assert said.text == "Would you like the terrace?" and said.heard == said.text
    assert said.triggered_by == ANA and not said.interrupted
    assert saids == [ended, said] and len(link.audio) == 1


async def test_a_tag_the_session_never_handed_out_is_the_vendors_own() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection.tagging = True
    async with session(link, live) as s:
        events = s.events()
        asked = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(
            LiveStarted(requested="elsewhere"),
            LiveSaid(text="Sure. "),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        own = await next_of(events, Said)
        waiting = not asked.done()
        live.connection.emit(
            LiveStarted(requested="request-1"),
            LiveSaid(text="Your table is ready."),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        said = await asked
    assert waiting and own.text == "Sure. " and own.triggered_by is None
    assert said.triggered_by == HOST and said.heard == "Your table is ready."


async def test_a_say_waits_for_the_response_before_it() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        asked = [
            asyncio.create_task(s.say(text, triggered_by=HOST))
            for text in ("One.", "Two.", "Three.")
        ]
        await until(lambda: len(live.connection.calls) == 1)
        await asyncio.sleep(0.01)
        sent_before = list(live.connection.calls)
        live.connection.emit(*response("One."))
        await until(lambda: len(live.connection.calls) == 2)
        live.connection.emit(*response("Two."))
        await until(lambda: len(live.connection.calls) == 3)
        live.connection.emit(*response("Three."))
        saids = await asyncio.gather(*asked)
    assert sent_before == [("say", "One.")]
    assert live.connection.calls == [("say", "One."), ("say", "Two."), ("say", "Three.")]
    assert [said.text for said in saids] == ["One.", "Two.", "Three."]


async def test_a_say_during_the_models_own_reply_waits_for_it() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(
            LiveStarted(), LiveSaid(text="Welcome. "), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 1)
        asked = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await asyncio.sleep(0.01)
        sent_during = list(live.connection.calls)
        live.connection.emit(LiveDone())
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(*response("Your table is ready."))
        own = await next_of(events, Said)
        said = await asked
    assert sent_during == [] and own.text == "Welcome. " and own.triggered_by is None
    assert said.triggered_by == HOST


async def test_mute_ends_the_says_not_yet_sent() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        live.connection.emit(LiveStarted(), LiveAudio(frame=pcm(100, 24000)))
        await until(lambda: len(link.audio) == 1)
        asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
        await asyncio.sleep(0.01)
        await s.mute(triggered_by=HOST)
        said = await asked
        live.connection.emit(LiveDone())
        await asyncio.sleep(0.01)
    assert said.heard == "" and said.interrupted and live.connection.calls == []


async def test_interrupt_by_code_keeps_the_says_not_yet_sent() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        live.connection.emit(
            LiveStarted(), LiveSaid(text="Welcome. "), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 1)
        asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
        await asyncio.sleep(0.01)
        await s.interrupt(triggered_by=HOST)
        kept = not asked.done() and live.connection.calls == []
        live.connection.emit(LiveDone())
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(*response("Hello."))
        said = await asked
    assert kept and said.heard == "Hello." and not said.interrupted


async def test_a_say_given_up_on_is_never_sent() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        live.connection.emit(LiveStarted(), LiveAudio(frame=pcm(100, 24000)))
        await until(lambda: len(link.audio) == 1)
        asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
        later = asyncio.create_task(s.say("Still there?", triggered_by=HOST))
        await asyncio.sleep(0.01)
        asked.cancel()
        live.connection.emit(LiveDone())
        await until(lambda: len(live.connection.calls) == 1)
        live.connection.emit(*response("Still there?"))
        await later
    assert live.connection.calls == [("say", "Still there?")]


async def test_close_ends_the_says_not_yet_sent() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    await s.start()
    live.connection.emit(LiveStarted(), LiveAudio(frame=pcm(100, 24000)))
    await until(lambda: len(link.audio) == 1)
    asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await asyncio.sleep(0.01)
    await s.close()
    with pytest.raises(TransportError):
        await asked
    assert live.connection.calls == []


async def test_quiet_waits_for_a_response_that_has_not_spoken_yet() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        await s.quiet()
        live.connection.emit(LiveStarted())
        await asyncio.sleep(0.01)
        quiet = asyncio.create_task(s.quiet())
        await asyncio.sleep(0.01)
        waiting = not quiet.done()
        live.connection.emit(
            LiveSaid(text="Goodbye."), LiveAudio(frame=pcm(100, 24000)), LiveDone()
        )
        await asyncio.wait_for(quiet, 1)
    assert waiting and {"type": "silent", "utterance": 1} in link.controls


async def test_quiet_waits_for_a_say_not_yet_answered() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        asked = asyncio.create_task(s.say("Goodbye.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 1)
        quiet = asyncio.create_task(s.quiet())
        await asyncio.sleep(0.01)
        waiting = not quiet.done()
        live.connection.emit(*response("Goodbye."))
        await asyncio.wait_for(quiet, 1)
        said = asked.result()
    assert waiting and said.heard == "Goodbye."


async def test_quiet_returns_when_the_session_closes() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    await s.start()
    live.connection.emit(LiveStarted())
    await asyncio.sleep(0.01)
    quiet = asyncio.create_task(s.quiet())
    await asyncio.sleep(0.01)
    await s.close()
    await asyncio.wait_for(quiet, 1)
    await asyncio.wait_for(s.quiet(), 1)
    with pytest.raises(TransportError, match="^session: start the session before using it$"):
        await session(Loopback(ANA, "acme"), ScriptedLive()).quiet()


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


async def test_a_response_started_while_one_is_open_fails_the_session() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(LiveStarted(), LiveStarted())
        closed = await next_of(events, Closed)
    assert closed.reason == "failed"
    assert closed.message == "session: a response started before the last one was done"


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


async def test_an_empty_stream_fails_its_say_and_reaches_no_model() -> None:
    async def blank() -> AsyncIterator[str]:
        yield "  "

    live = ScriptedLive()
    async with Session(Loopback(ANA, "acme"), live=live) as s:
        with pytest.raises(InputError, match="^session: text is empty$"):
            await s.say(blank(), triggered_by=HOST)
    assert live.connection.calls == []


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


def consulting() -> tuple[Tools, asyncio.Event, asyncio.Event]:
    started, gate = asyncio.Event(), asyncio.Event()

    async def consult(args: dict[str, Any]) -> str:
        started.set()
        await gate.wait()
        return "facts"

    return Tools([Tool("consult", "Ask", body=consult)]), started, gate


async def test_a_call_the_model_withdraws_is_never_answered() -> None:
    tools, started, gate = consulting()
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    await s.start(tools=tools)
    async with s:
        events = s.events()
        live.connection.emit(LiveToolCall(call_id="c1", name="consult"))
        await asyncio.wait_for(started.wait(), 1)
        live.connection.emit(LiveToolCancelled(call_ids=["c1"]))
        called = await next_of(events, ToolCalled)
        gate.set()
        await asyncio.sleep(0.01)
    rest = [e async for e in events]
    assert live.connection.results == []
    assert called.cancelled and called.output == "" and not called.is_error
    assert not [e for e in rest if isinstance(e, Cancelled)]


async def test_cancel_answers_every_running_call_without_a_reply() -> None:
    tools, started, _ = consulting()
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    await s.start(tools=tools)
    async with s:
        events = s.events()
        live.connection.emit(LiveToolCall(call_id="c1", name="consult"))
        await asyncio.wait_for(started.wait(), 1)
        cancelled = await s.cancel(triggered_by=HOST)
        answered = list(live.connection.results)
        first = await next_of(events, Cancelled)
        called = await next_of(events, ToolCalled)
    assert cancelled is not None and cancelled.tools == ["consult"]
    assert cancelled.triggered_by == HOST and first == cancelled
    assert answered == [("c1", "cancelled", True, False)]
    assert called.cancelled and called.is_error and called.output == "cancelled"
    assert not [
        c for c in link.controls if c["type"] == "event" and c["event"]["kind"] == "cancelled"
    ]


async def test_cancel_with_nothing_running_returns_none() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        assert await s.cancel(triggered_by=HOST) is None
    async with Session(Loopback(ANA, "acme"), speech=Tone()) as spoken:
        with pytest.raises(ConfigurationError, match="^session: cancel needs a live model$"):
            await spoken.cancel(triggered_by=HOST)


async def test_cancel_from_inside_a_tool_spares_that_tool() -> None:
    tools, started, _ = consulting()
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)

    async def stop(args: dict[str, Any]) -> str:
        cancelled = await s.cancel(triggered_by=HOST)
        return f"stopped {cancelled.tools if cancelled else []}"

    await s.start(tools=tools + Tools([Tool("stop", "Stop", body=stop)]))
    async with s:
        events = s.events()
        live.connection.emit(LiveToolCall(call_id="c1", name="consult"))
        await asyncio.wait_for(started.wait(), 1)
        live.connection.emit(LiveToolCall(call_id="c2", name="stop"))
        await next_of(events, ToolCalled)
        await next_of(events, ToolCalled)
    assert sorted(live.connection.results) == [
        ("c1", "cancelled", True, False),
        ("c2", "stopped ['consult']", False, True),
    ]


async def test_a_withdrawal_for_an_unknown_call_is_ignored() -> None:
    async def terrace(args: dict[str, Any]) -> str:
        return "open"

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    await s.start(tools=Tools([Tool("terrace", "Open?", body=terrace)]))
    async with s:
        events = s.events()
        live.connection.emit(LiveToolCancelled(call_ids=["ghost"]))
        live.connection.emit(LiveToolCall(call_id="c1", name="terrace"))
        await next_of(events, ToolCalled)
        live.connection.emit(LiveToolCancelled(call_ids=["c1"]), *response("Open."))
        said = await next_of(events, Said)
    assert live.connection.results == [("c1", "open", False, True)] and said.text == "Open."


async def test_a_tool_that_closes_the_session_closes_it() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)

    async def end_call(args: dict[str, Any]) -> str:
        await asyncio.sleep(0.01)
        await s.close()
        return "ended"

    await s.start(tools=Tools([Tool("end_call", "End", body=end_call)]), inform=["end_call"])
    events = s.events()
    live.connection.emit(LiveToolCall(call_id="c1", name="end_call"))
    seen = await asyncio.wait_for(collect_all(events), 1)
    await until(lambda: link.closed)
    assert isinstance(seen[-1], Closed) and seen[-1].reason == "closed"
    assert live.connection.closed


async def collect_all(events: Any) -> list[Any]:
    return [e async for e in events]


@pytest.mark.parametrize("end", ["failed", "ended"])
async def test_a_say_held_when_the_connection_ends_raises(end: str) -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    await s.start()
    live.connection.emit(LiveStarted(), LiveAudio(frame=pcm(100, 24000)))
    await until(lambda: len(link.audio) == 1)
    held = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await asyncio.sleep(0.01)
    if end == "failed":
        live.connection.emit(LiveDone(), LiveFailed(message="gone"))
    else:
        live.connection.emit(LiveDone())
        live.connection.emit_end()
    with pytest.raises(ModelError):
        await asyncio.wait_for(held, 1)


class StuckSay(ScriptedConnection):
    async def say(self, text: str) -> None:
        await super().say(text)
        await asyncio.Event().wait()


async def test_close_fails_a_say_held_behind_one_a_tool_is_sending() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = StuckSay()
    s = session(link, live)

    async def lookup(args: dict[str, Any]) -> str:
        await s.say("One moment.", triggered_by=HOST)
        return "done"

    await s.start(tools=Tools([Tool("lookup", "Look", body=lookup)]))
    live.connection.emit(LiveToolCall(call_id="c1", name="lookup"))
    await until(lambda: len(live.connection.calls) == 1)
    held = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await asyncio.sleep(0.01)
    await s.close()
    with pytest.raises(TransportError, match="^session: the session closed before the reply"):
        await asyncio.wait_for(held, 1)
    assert live.connection.calls == [("say", "One moment.")]


async def test_mute_ends_a_sent_say_the_vendor_never_answered() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        sent = asyncio.create_task(s.say("First.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 1)
        held = asyncio.create_task(s.say("Second.", triggered_by=HOST))
        await asyncio.sleep(0.01)
        await s.mute(triggered_by=HOST)
        ended = await asyncio.wait_for(asyncio.gather(sent, held), 1)
        await s.unmute(triggered_by=HOST)
        after = asyncio.create_task(s.say("Third.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 2)
        live.connection.emit(*response("Third."))
        said = await after
    assert all(e.interrupted and e.heard == "" for e in ended)
    assert live.connection.calls == [("say", "First."), ("say", "Third.")]
    assert said.heard == "Third." and not said.interrupted


class SendingSay(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.sent = asyncio.Event()
        self.sending = 0
        self.overlapped = False
        self.refusal: Exception | None = None
        self.returning: Callable[[], object] | None = None

    async def say(self, text: str) -> str | None:
        self.sending += 1
        self.overlapped = self.overlapped or self.sending > 1
        try:
            tag = await super().say(text)
            await self.sent.wait()
            refusal, self.refusal = self.refusal, None
            if refusal is not None:
                raise refusal
            returning, self.returning = self.returning, None
            if returning is not None:
                asyncio.get_running_loop().call_soon(returning)
            return tag
        finally:
            self.sending -= 1


async def test_a_late_answer_to_a_say_muted_while_it_was_sending_is_cut() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = SendingSay()
    live.connection.tagging = True
    async with session(link, live) as s:
        events = s.events()
        first = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(live.connection.calls) == 1)
        await s.mute(triggered_by=HOST)
        await s.unmute(triggered_by=HOST)
        live.connection.sent.set()
        ended = await asyncio.wait_for(first, 1)
        live.connection.emit(
            LiveStarted(id="late", requested="request-1"),
            LiveSaid(text="Your table is ready."),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        await until(lambda: bool(live.connection.interrupts))
        await s.quiet()
    saids = [e async for e in events if isinstance(e, Said)]
    assert ended.heard == "" and ended.interrupted and saids == [ended]
    assert live.connection.interrupts == [(0, "", "late")] and link.audio == []


async def test_a_late_answer_to_a_say_given_up_while_it_was_sending_is_cut() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = connection = SendingSay()
    connection.tagging = True
    async with session(link, live) as s:
        events = s.events()
        first = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(connection.calls) == 1)
        first.cancel()
        second = asyncio.create_task(s.say("Shall I order the usual?", triggered_by=ANA))
        await asyncio.sleep(0.01)
        sent_while_sending = list(connection.calls)
        connection.sent.set()
        await until(lambda: len(connection.calls) == 2)
        connection.emit(
            LiveStarted(id="late", requested="request-1"),
            LiveSaid(text="Your table is ready."),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
            LiveStarted(id="own", requested="request-2"),
            LiveSaid(text="Shall I order the usual?"),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        said = await asyncio.wait_for(second, 1)
    saids = [e async for e in events if isinstance(e, Said)]
    assert first.cancelled() and not connection.overlapped
    assert sent_while_sending == [("say", "Your table is ready.")]
    assert connection.interrupts == [(0, "", "late")]
    assert said.triggered_by == ANA and said.heard == "Shall I order the usual?"
    assert saids == [said] and len(link.audio) == 1


async def test_a_say_given_up_as_its_send_returns_has_its_late_answer_cut() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = connection = SendingSay()
    connection.tagging = True
    async with session(link, live) as s:
        events = s.events()
        first = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(connection.calls) == 1)
        connection.returning = first.cancel
        connection.sent.set()
        await until(first.done)
        connection.emit(
            LiveStarted(id="late", requested="request-1"),
            LiveSaid(text="Your table is ready."),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        await until(lambda: bool(connection.interrupts))
        await s.quiet()
    saids = [e async for e in events if isinstance(e, Said)]
    assert first.cancelled() and saids == [] and link.audio == []
    assert connection.interrupts == [(0, "", "late")]


async def test_a_say_behind_one_given_up_while_sending_waits_for_the_open_response() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = connection = SendingSay()
    async with session(link, live) as s:
        first = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(connection.calls) == 1)
        first.cancel()
        second = asyncio.create_task(s.say("Shall I order the usual?", triggered_by=ANA))
        connection.emit(
            LiveStarted(requested=False), LiveSaid(text="Sure. "), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 1)
        connection.sent.set()
        await until(lambda: connection.sending == 0)
        await asyncio.sleep(0.01)
        sent_while_open = list(connection.calls)
        connection.emit(LiveDone())
        await until(lambda: len(connection.calls) == 2)
        connection.emit(*response("Shall I order the usual?"))
        said = await asyncio.wait_for(second, 1)
    assert sent_while_open == [("say", "Your table is ready.")]
    assert said.triggered_by == ANA and said.heard == "Shall I order the usual?"


async def test_a_send_that_fails_after_its_caller_gave_up_is_logged_once(
    caplog: pytest.LogCaptureFixture,
) -> None:
    loop = asyncio.get_running_loop()
    handled: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _, context: handled.append(context))
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = connection = SendingSay()
    try:
        async with session(link, live) as s:
            first = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
            await until(lambda: len(connection.calls) == 1)
            first.cancel()
            connection.refusal = RuntimeError("the vendor refused")
            connection.sent.set()
            second = asyncio.create_task(s.say("Shall I order the usual?", triggered_by=ANA))
            await until(lambda: len(connection.calls) == 2)
            connection.emit(*response("Shall I order the usual?"))
            said = await asyncio.wait_for(second, 1)
        gc.collect()
        await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    failures = [r for r in caplog.records if r.name == "rfnry_senses.session"]
    assert [c["message"] for c in handled] == []
    assert [(r.levelname, r.getMessage()) for r in failures] == [
        ("WARNING", "session: a request given up on failed to send: the vendor refused")
    ]
    assert said.heard == "Shall I order the usual?"


async def test_a_close_while_a_say_is_sending_fails_it() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = connection = SendingSay()
    s = session(link, live)
    await s.start()
    events = s.events()
    asked = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
    await until(lambda: len(connection.calls) == 1)
    await asyncio.wait_for(s.close(), 1)
    with pytest.raises(TransportError, match="^session: the session closed before the reply"):
        await asyncio.wait_for(asked, 1)
    closed = await next_of(events, Closed)
    assert closed.reason == "closed" and connection.closed and connection.sending == 0


async def test_a_say_during_a_barge_in_cut_is_never_sent() -> None:
    live, link = ScriptedLive(), AwaitingLoopback(ANA, "acme", answer=False)
    async with session(link, live) as s:
        live.connection.emit(
            LiveStarted(), LiveSaid(text="Welcome. "), LiveAudio(frame=pcm(400, 24000)), LiveDone()
        )
        await until(lambda: len(link.audio) == 1)
        live.connection.emit(LiveInterrupted())
        await until(lambda: any(c["type"] == "stop" for c in link.controls))
        said = await asyncio.wait_for(s.say("One moment.", triggered_by=HOST), 2)
    assert said.heard == "" and said.interrupted
    assert live.connection.calls == []


async def test_a_say_issued_on_interrupted_waits_for_the_next_turn() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme", answer=False)
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(
            LiveStarted(), LiveSaid(text="Welcome. "), LiveAudio(frame=pcm(100, 24000)), LiveDone()
        )
        await until(lambda: len(link.audio) == 1)
        live.connection.emit(LiveInterrupted())
        await next_of(events, Interrupted)
        asked = asyncio.create_task(s.say("Go ahead.", triggered_by=HOST))
        await until(lambda: bool(live.connection.calls))
        await asyncio.sleep(0.01)
        held = not asked.done()
        live.connection.emit(*response("Go ahead."))
        said = await asyncio.wait_for(asked, 3)
    assert held and live.connection.calls == [("say", "Go ahead.")]
    assert said.heard == "Go ahead." and not said.interrupted and said.triggered_by == HOST


async def test_a_withdrawal_as_the_body_returns_still_reports_what_it_returned() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)

    async def book(args: dict[str, Any]) -> str:
        await asyncio.sleep(0.01)
        live.connection.emit(LiveToolCancelled(call_ids=["c1"]))
        return "booked table 4"

    await s.start(tools=Tools([Tool("book", "Book", body=book)]))
    async with s:
        events = s.events()
        live.connection.emit(LiveToolCall(call_id="c1", name="book"))
        called = await next_of(events, ToolCalled)
    assert live.connection.results == []
    assert called.cancelled and called.output == "booked table 4" and not called.is_error


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


class BlindConnection(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.images_tried = 0

    async def send_image(self, image: Image) -> None:
        self.images_tried += 1
        raise RuntimeError("this model takes no images")


class BlindLive(ScriptedLive):
    def __init__(self) -> None:
        super().__init__()
        self.connection: BlindConnection = BlindConnection()


async def test_a_live_model_that_takes_no_images_keeps_the_session(
    caplog: pytest.LogCaptureFixture,
) -> None:
    live, link = BlindLive(), Loopback(ANA, "acme")
    async with session(link, live, video=True) as s:
        await link.show(JPEG)
        await until(lambda: live.connection.images_tried == 1)
        await asyncio.sleep(0.01)
        await link.show(JPEG + b"1")
        await link.show(JPEG + b"2")
        await asyncio.sleep(0.02)
        parts = await s.look(frames=3, triggered_by=HOST)
        assert not s._closed
    assert live.connection.images_tried == 1
    assert len(parts) == 6
    assert len([r for r in caplog.records if "image" in r.getMessage()]) == 1


class FailingLine(Loopback):
    async def send_audio(self, frame: AudioFrame, utterance: int) -> None:
        raise ConnectionError("the phone line dropped")


async def test_a_transport_send_failure_is_a_transport_error() -> None:
    live, link = ScriptedLive(), FailingLine(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(*response("Hi. "))
        closed = await next_of(events, Closed)
    assert closed.reason == "failed" and closed.message is not None
    assert closed.message.startswith("session: the transport failed")


async def test_a_waiting_say_raises_transport_error_when_a_send_fails() -> None:
    live, link = ScriptedLive(), FailingLine(ANA, "acme")
    s = session(link, live)
    await s.start()
    live.connection.emit(LiveStarted())
    await asyncio.sleep(0.01)
    saying = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
    await asyncio.sleep(0.01)
    live.connection.emit(LiveAudio(frame=pcm(100, 24000)))
    with pytest.raises(TransportError, match="^session: the transport failed: "):
        await asyncio.wait_for(saying, 1)


async def test_an_invalid_tool_definition_is_one_line() -> None:
    class Broken:
        def definitions(self) -> list[dict[str, Any]]:
            return [{"name": "broken"}]

        async def dispatch(self, name: str, args: dict[str, Any]) -> str:
            return ""

    s = session(Loopback(ANA, "acme"), ScriptedLive())
    with pytest.raises(InputError) as raised:
        await s.start(tools=Broken())
    assert "\n" not in str(raised.value) and "description" in str(raised.value)


async def test_an_invalid_tool_definition_raises_input_error() -> None:
    class Broken:
        def definitions(self) -> list[dict[str, Any]]:
            return [{"name": "x"}]

    s = session(Loopback(ANA, "acme"), ScriptedLive())
    with pytest.raises(InputError, match="^session: a tool definition is not valid"):
        await s.start(tools=Broken())  # type: ignore[arg-type]


async def test_closed_names_who_ended_the_session() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        await s.close(triggered_by=HOST)
        assert (await next_of(events, Closed)).triggered_by == HOST

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        await link.hang_up()
        closed = await next_of(events, Closed)
        assert closed.reason == "hung_up" and closed.triggered_by == ANA

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(LiveFailed(message="gone"))
        closed = await next_of(events, Closed)
        assert closed.reason == "failed" and closed.triggered_by is None

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)
    async with s:
        events = s.events()
    assert (await next_of(events, Closed)).triggered_by is None

    live, link = ScriptedLive(), Loopback(ANA, "acme")
    s = session(link, live)

    async def end_call(args: dict[str, Any]) -> str:
        await s.close(triggered_by=ANA)
        return "ended"

    await s.start(tools=Tools([Tool("end_call", "End", body=end_call)]), inform=["end_call"])
    events = s.events()
    live.connection.emit(LiveToolCall(call_id="c1", name="end_call"))
    assert (await next_of(events, Closed)).triggered_by == ANA


class DeafLine(Loopback):
    async def send_control(self, control: dict[str, Any]) -> None:
        if control["type"] == "ready":
            raise ConnectionError("the phone line dropped")
        await super().send_control(control)


async def test_a_transport_that_fails_the_ready_closes_the_session_as_failed() -> None:
    s = session(DeafLine(ANA, "acme"), ScriptedLive())
    events = s.events()
    with pytest.raises(TransportError, match="^session: the transport failed: "):
        await s.start()
    closed = await next_of(events, Closed)
    assert closed.reason == "failed" and closed.triggered_by is None


class StuckLine(Loopback):
    def clear_audio(self) -> None:
        raise ConnectionError("the phone line dropped")


async def test_a_transport_that_fails_to_clear_audio_closes_the_session_as_failed() -> None:
    live, link = ScriptedLive(), StuckLine(ANA, "acme")
    async with session(link, live) as s:
        events = s.events()
        live.connection.emit(LiveStarted(), LiveAudio(frame=pcm(100, 24000)))
        await until(lambda: len(link.audio) == 1)
        with pytest.raises(TransportError, match="^session: the transport failed: "):
            await s.interrupt(triggered_by=ANA)
        closed = await next_of(events, Closed)
    assert closed.reason == "failed"


async def test_a_say_granted_as_the_person_cuts_in_is_never_sent() -> None:
    live, link = ScriptedLive(), SlowLoopback(ANA, "acme", delay=None)
    connection = live.connection
    async with session(link, live) as s:
        first = asyncio.create_task(s.say("First.", triggered_by=HOST))
        await until(lambda: len(connection.calls) == 1)
        connection.emit(
            LiveStarted(requested=True), LiveSaid(text="First."), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: any(c["type"] == "speaking" for c in link.controls))
        second = asyncio.create_task(s.say("Second.", triggered_by=HOST))
        await asyncio.sleep(0.01)
        connection.emit(LiveDone(), LiveInterrupted())
        said = await asyncio.wait_for(second, 1)
        await asyncio.wait_for(first, 1)
        await asyncio.sleep(0.01)
    assert said.heard == "" and said.interrupted
    assert connection.calls == [("say", "First.")]


class HeldInform(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()
        self.informing = False

    async def send_text(self, text: str, *, respond: bool) -> str | None:
        if not respond:
            self.informing = True
            await self.gate.wait()
        return await super().send_text(text, respond=respond)


async def playing_with_a_say_behind_an_inform(
    s: Session, link: SlowLoopback, connection: HeldInform
) -> tuple[asyncio.Task[Said], asyncio.Task[None], asyncio.Task[Said]]:
    first = asyncio.create_task(s.say("First.", triggered_by=HOST))
    await until(lambda: len(connection.calls) == 1)
    connection.emit(
        LiveStarted(requested=True),
        LiveSaid(text="First."),
        LiveAudio(frame=pcm(1000, 24000)),
        LiveDone(),
    )
    await until(lambda: any(c["type"] == "silent" for c in link.controls))
    informing = asyncio.create_task(s.inform("A fact.", triggered_by=HOST))
    await until(lambda: connection.informing)
    second = asyncio.create_task(s.say("Second.", triggered_by=HOST))
    await asyncio.sleep(0.01)
    return first, informing, second


async def test_a_say_behind_an_inform_is_not_sent_when_the_person_cuts_in() -> None:
    live, link = ScriptedLive(), SlowLoopback(ANA, "acme", delay=None)
    live.connection = connection = HeldInform()
    async with session(link, live) as s:
        first, informing, second = await playing_with_a_say_behind_an_inform(s, link, connection)
        connection.emit(LiveInterrupted())
        await until(lambda: any(c["type"] == "stop" for c in link.controls))
        connection.gate.set()
        await informing
        said = await asyncio.wait_for(second, 1)
        await asyncio.wait_for(first, 1)
        await asyncio.sleep(0.01)
    assert said.heard == "" and said.interrupted
    assert [call[0] for call in connection.calls] == ["say", "text"]


async def test_a_say_ended_while_it_waits_behind_an_inform_returns_at_once() -> None:
    live, link = ScriptedLive(), SlowLoopback(ANA, "acme", delay=None)
    live.connection = connection = HeldInform()
    async with session(link, live) as s:
        first, informing, second = await playing_with_a_say_behind_an_inform(s, link, connection)
        connection.emit(LiveInterrupted())
        said = await asyncio.wait_for(second, 1)
        connection.gate.set()
        await informing
        await asyncio.wait_for(first, 1)
    assert said.heard == "" and said.interrupted


async def test_a_say_behind_an_inform_waits_out_a_cut_it_was_not_ended_by() -> None:
    live, link = ScriptedLive(), SlowLoopback(ANA, "acme", delay=None)
    live.connection = connection = HeldInform()
    async with session(link, live) as s:
        first, informing, second = await playing_with_a_say_behind_an_inform(s, link, connection)
        cutting = asyncio.create_task(s.interrupt(triggered_by=HOST))
        await until(lambda: any(c["type"] == "stop" for c in link.controls))
        connection.gate.set()
        await informing
        await asyncio.sleep(0.01)
        during = list(connection.calls)
        await asyncio.wait_for(cutting, 1)
        await until(lambda: len(connection.calls) == 3)
        connection.emit(*response("Second."))
        said = await asyncio.wait_for(second, 3)
        await asyncio.wait_for(first, 1)
    assert [call[0] for call in during] == ["say", "text"]
    assert said.heard == "Second." and not said.interrupted


async def test_a_say_due_as_the_model_starts_its_own_reply_waits_for_that_reply() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    connection = live.connection
    async with session(link, live) as s:
        first = asyncio.create_task(s.say("First.", triggered_by=HOST))
        await until(lambda: len(connection.calls) == 1)
        connection.emit(
            LiveStarted(requested=True), LiveSaid(text="First."), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 1)
        second = asyncio.create_task(s.say("Second.", triggered_by=HOST))
        await asyncio.sleep(0.01)
        connection.emit(
            LiveDone(),
            LiveStarted(requested=False),
            LiveSaid(text="By the way. "),
            LiveAudio(frame=pcm(100, 24000)),
        )
        await until(lambda: len(link.audio) == 2)
        await asyncio.sleep(0.01)
        while_open = list(connection.calls)
        connection.emit(LiveDone())
        await until(lambda: len(connection.calls) == 2)
        connection.emit(*response("Second."))
        said = await asyncio.wait_for(second, 1)
        await asyncio.wait_for(first, 1)
    assert while_open == [("say", "First.")]
    assert said.heard == "Second." and said.triggered_by == HOST


async def test_a_say_issued_while_a_barge_in_reports_its_queue_is_ended_with_it() -> None:
    live, link = ScriptedLive(), SlowEvents(ANA, "acme")
    connection = live.connection
    async with session(link, live) as s:
        connection.emit(LiveStarted(), LiveSaid(text="Hi. "), LiveAudio(frame=pcm(100, 24000)))
        await until(lambda: len(link.audio) == 1)
        queued = asyncio.create_task(s.say("Queued.", triggered_by=HOST))
        await asyncio.sleep(0.01)
        connection.emit(LiveInterrupted())
        await until(lambda: link.at_gate)
        late = asyncio.create_task(s.say("Late.", triggered_by=HOST))
        await asyncio.sleep(0.01)
        link.gate.set()
        connection.emit(LiveDone())
        said = await asyncio.wait_for(late, 2)
        await asyncio.wait_for(queued, 1)
    assert said.heard == "" and said.interrupted
    assert connection.calls == []


class SlowEvents(Loopback):
    def __init__(self, identity: Any, scope: str) -> None:
        super().__init__(identity, scope)
        self.gate = asyncio.Event()
        self.at_gate = False

    async def send_control(self, control: dict[str, Any]) -> None:
        if control["type"] == "event" and control["event"]["kind"] == "said":
            self.at_gate = True
            await self.gate.wait()
        await super().send_control(control)


async def test_the_queue_a_live_barge_in_ends_is_reported_before_the_interruption() -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    connection = live.connection
    async with session(link, live) as s:
        events = s.events()
        first = asyncio.create_task(s.say("First.", triggered_by=HOST))
        await until(lambda: len(connection.calls) == 1)
        connection.emit(
            LiveStarted(requested=True), LiveSaid(text="First."), LiveAudio(frame=pcm(100, 24000))
        )
        await until(lambda: len(link.audio) == 1)
        second = asyncio.create_task(s.say("Second.", triggered_by=HOST))
        await asyncio.sleep(0.01)
        connection.emit(LiveInterrupted())
        await asyncio.wait_for(asyncio.gather(first, second), 1)
    told = [
        f"said:{e.text}" if isinstance(e, Said) else type(e).__name__
        async for e in events
        if isinstance(e, Said | Interrupted)
    ]
    assert told == ["said:Second.", "Interrupted", "said:First."]


class EarlyTag(ScriptedConnection):
    async def say(self, text: str) -> str | None:
        self.calls.append(("say", text))
        self.emit(LiveStarted(id="r1", requested="early"))
        await asyncio.sleep(0.01)
        return "early"


async def test_a_late_answer_landing_during_another_send_warns_of_nothing(
    caplog: pytest.LogCaptureFixture,
) -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = connection = SendingSay()
    connection.tagging = True
    async with session(link, live) as s:
        first = asyncio.create_task(s.say("Your table is ready.", triggered_by=HOST))
        await until(lambda: len(connection.calls) == 1)
        first.cancel()
        connection.sent.set()
        await until(lambda: connection.sending == 0)
        connection.sent.clear()
        second = asyncio.create_task(s.say("Shall I order the usual?", triggered_by=ANA))
        await until(lambda: len(connection.calls) == 2)
        connection.emit(
            LiveStarted(id="late", requested="request-1"),
            LiveSaid(text="Your table is ready."),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        await until(lambda: bool(connection.interrupts))
        connection.sent.set()
        await until(lambda: connection.sending == 0)
        connection.emit(
            LiveStarted(id="own", requested="request-2"),
            LiveSaid(text="Shall I order the usual?"),
            LiveAudio(frame=pcm(100, 24000)),
            LiveDone(),
        )
        said = await asyncio.wait_for(second, 1)
    assert connection.interrupts == [(0, "", "late")]
    assert said.triggered_by == ANA and said.heard == "Shall I order the usual?"
    assert not any("before its send returned" in m for m in caplog.messages)


async def test_a_tag_that_lands_before_its_send_returns_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    live, link = ScriptedLive(), Loopback(ANA, "acme")
    live.connection = EarlyTag()
    async with session(link, live) as s:
        asked = asyncio.create_task(s.say("Hello.", triggered_by=HOST))
        await until(lambda: any("early" in m for m in caplog.messages))
        live.connection.emit(LiveDone())
        await s.mute(triggered_by=HOST)
        await asyncio.wait_for(asked, 1)
    assert [m for m in caplog.messages if "early" in m] == [
        "session: a response named tag 'early' before its send returned; "
        "a tag goes on its response only after that"
    ]

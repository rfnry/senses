import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from rfnry_senses.contracts import LiveDone, LiveFailed, LiveHeard, LiveToolCall
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
    results = {call_id: (result, is_error) for call_id, result, is_error in live.connection.results}
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

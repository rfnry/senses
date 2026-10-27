import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import ConfigurationError, ModelError
from rfnry_senses.events import Closed, Heard, Silent, Speaking, TurnEnded
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, ScriptedTranscription, Tone, next_of, pcm, until


def cascaded(**kwargs: Any) -> tuple[Session, Loopback, ScriptedTranscription, Tone]:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    return Session(link, transcription=stt, speech=tts, **kwargs), link, stt, tts


async def test_the_person_is_heard_and_their_turn_ends_with_the_utterance() -> None:
    s, link, stt, _ = cascaded()
    async with s:
        events = s.events()
        await link.speak(pcm(20))
        stt.hear("a table", final=False, end=False)
        stt.hear("A table for two.", "At eight.")
        heard = [await next_of(events, Heard) for _ in range(3)]
        turn = await next_of(events, TurnEnded)
        await until(lambda: len(stt.fed) == 1)
    assert [h.final for h in heard] == [False, True, True]
    assert turn.text == "A table for two. At eight." and turn.speaker == ANA


async def test_a_trailing_partial_belongs_to_the_turn() -> None:
    s, _, stt, _ = cascaded()
    async with s:
        events = s.events()
        stt.hear("book a", final=False)
        assert (await next_of(events, TurnEnded)).text == "book a"


async def test_quiet_waits_for_the_cascaded_queue() -> None:
    s, _, _, _ = cascaded()
    async with s:
        first = asyncio.create_task(s.say("One two. Three.", triggered_by=HOST))
        second = asyncio.create_task(s.say("Four.", triggered_by=HOST))
        await asyncio.sleep(0)
        await asyncio.wait_for(s.quiet(), 1)
        done = first.done() and second.done()
    assert done


async def test_say_speaks_sentence_by_sentence_and_in_order() -> None:
    s, link, _, tts = cascaded()
    async with s:
        first = asyncio.create_task(s.say("One two. Three.", triggered_by=HOST))
        second = asyncio.create_task(s.say("Four.", triggered_by=ANA))
        said_first, said_second = await first, await second
    assert tts.spoken == ["One two. ", "Three.", "Four."]
    assert (
        said_first.heard == "One two. Three."
        and said_first.triggered_by == HOST
        and not said_first.interrupted
    )
    assert said_second.heard == "Four." and len(link.audio) == 4
    kinds = [
        (c["type"], c.get("utterance"))
        for c in link.controls
        if c["type"] in ("speaking", "silent")
    ]
    assert kinds == [("speaking", 1), ("silent", 1), ("speaking", 2), ("silent", 2)]


async def test_a_failing_speech_model_fails_only_its_say() -> None:
    s, _, _, tts = cascaded()
    async with s:
        tts.fail = True
        with pytest.raises(
            ModelError, match="^session: the speech model failed: synthesis failed$"
        ):
            await s.say("Hello.", triggered_by=HOST)
        tts.fail = False
        assert (await s.say("Hello again.", triggered_by=HOST)).heard == "Hello again."


class FailsOnSecond(Tone):
    async def speak(self, text: str) -> AsyncIterator[AudioFrame]:
        self.fail = bool(self.spoken)
        async for frame in super().speak(text):
            yield frame


async def test_a_speech_model_failing_after_the_voice_started_still_ends_it() -> None:
    link = Loopback(ANA, "acme")
    async with Session(link, speech=FailsOnSecond()) as s:
        events = s.events()
        with pytest.raises(
            ModelError, match="^session: the speech model failed: synthesis failed$"
        ):
            await s.say("One two. Three four.", triggered_by=HOST)
    assert [type(event) async for event in events] == [Speaking, Silent, Closed]
    assert [c for c in link.controls if c["type"] in ("speaking", "silent", "stop")] == [
        {"type": "speaking", "utterance": 1},
        {"type": "silent", "utterance": 1},
    ]


async def test_each_model_is_optional() -> None:
    stt = ScriptedTranscription()
    async with Session(Loopback(ANA, "acme"), transcription=stt) as listener:
        with pytest.raises(
            ConfigurationError, match="^session: say needs a live model or a speech model$"
        ):
            await listener.say("Hello.", triggered_by=HOST)
        with pytest.raises(ConfigurationError, match="^session: prompt needs a live model$"):
            await listener.prompt("Hello.", triggered_by=HOST)
        await listener.mute(triggered_by=HOST)
        with pytest.raises(
            ConfigurationError, match="^session: say needs a live model or a speech model$"
        ):
            await listener.say("Hello.", triggered_by=HOST)
        await stt.close()
        await asyncio.sleep(0.01)
        with pytest.raises(
            ConfigurationError, match="^session: interrupt needs a live model or a speech model$"
        ):
            await listener.interrupt(triggered_by=HOST)
    async with Session(Loopback(ANA, "acme"), speech=Tone()) as announcer:
        said = await announcer.say("Doors close in one minute.", triggered_by=HOST)
    assert said.heard == "Doors close in one minute."


async def test_say_speaks_a_stream_as_its_sentences_complete() -> None:
    s, link, _, tts = cascaded()
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def reply() -> AsyncIterator[str]:
        while (chunk := await queue.get()) is not None:
            yield chunk

    async with s:
        saying = asyncio.create_task(s.say(reply(), triggered_by=HOST))
        queue.put_nowait("One two")
        queue.put_nowait(". Thr")
        await until(lambda: tts.spoken == ["One two. "])
        queue.put_nowait("ee.")
        queue.put_nowait(None)
        said = await saying
    assert tts.spoken == ["One two. ", "Three."] and len(link.audio) == 3
    assert (
        said.text == said.heard == "One two. Three."
        and not said.interrupted
        and said.triggered_by == HOST
    )


async def test_an_empty_stream_says_nothing_and_a_failing_one_fails_only_its_say() -> None:
    async def empty() -> AsyncIterator[str]:
        nothing: tuple[str, ...] = ()
        for chunk in nothing:
            yield chunk

    async def broken() -> AsyncIterator[str]:
        yield "One. "
        raise RuntimeError("the writer failed")

    s, _, _, tts = cascaded()
    async with s:
        nothing = await s.say(empty(), triggered_by=HOST)
        with pytest.raises(RuntimeError, match="^the writer failed$"):
            await s.say(broken(), triggered_by=HOST)
        after = await s.say("Still here.", triggered_by=HOST)
    assert nothing.text == nothing.heard == "" and not nothing.interrupted
    assert after.heard == "Still here." and tts.spoken == ["Still here."]


async def test_cascaded_utterances_each_start_and_end_the_voice() -> None:
    s, _, _, _ = cascaded()
    async with s:
        events = s.events()
        first = asyncio.create_task(s.say("One.", triggered_by=HOST))
        second = asyncio.create_task(s.say("Two.", triggered_by=HOST))
        await first, await second
        kinds = [type(await anext(events)).__name__ for _ in range(6)]
    assert kinds == ["Speaking", "Silent", "Said", "Speaking", "Silent", "Said"]

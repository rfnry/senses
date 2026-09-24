import asyncio
from collections.abc import AsyncIterator

from rfnry_senses.events import Heard, Interrupted
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .fakes import ANA, HOST, ScriptedTranscription, Tone, next_of, until


async def test_the_person_talking_over_the_voice_cuts_it_and_the_queue() -> None:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    tts.held = "Three four."
    async with Session(link, transcription=stt, speech=tts) as s:
        events = s.events()
        first = asyncio.create_task(s.say("One two. Three four.", triggered_by=HOST))
        queued = asyncio.create_task(s.say("Five.", triggered_by=HOST))
        await until(lambda: len(link.audio) == 2 and "Three four." in tts.spoken)
        stt.hear("wait", final=False, end=False)
        cut = await next_of(events, Interrupted)
        heard = await next_of(events, Heard)
        said_first, said_queued = await first, await queued
    assert cut.heard == "One two. " and cut.triggered_by == ANA and heard.text == "wait"
    assert said_first.interrupted and said_first.heard == "One two. "
    assert said_queued.interrupted and said_queued.heard == "" and "Five." not in tts.spoken
    assert {"type": "stop", "utterance": 1} in link.controls


async def test_a_sentence_finished_after_barge_in_is_never_sent() -> None:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    tts.held = "Three four."
    async with Session(link, transcription=stt, speech=tts) as s:
        events = s.events()
        saying = asyncio.create_task(s.say("One two. Three four.", triggered_by=HOST))
        await until(lambda: "Three four." in tts.spoken)
        stt.hear("stop", end=False)
        await next_of(events, Interrupted)
        tts.release.set()
        said = await saying
        await asyncio.sleep(0.02)
    assert len(link.audio) == 2 and said.heard == "One two. "
    assert all(c.get("utterance") == 1 for c in link.controls if c["type"] in ("speaking", "stop"))


async def test_the_voice_speaks_again_after_a_barge_in() -> None:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    tts.held = "Three four."
    async with Session(link, transcription=stt, speech=tts) as s:
        events = s.events()
        asyncio.create_task(s.say("One two. Three four.", triggered_by=HOST))
        await until(lambda: "Three four." in tts.spoken)
        stt.hear("sorry")
        await next_of(events, Interrupted)
        said = await s.say("Go ahead.", triggered_by=HOST)
    assert said.heard == "Go ahead." and not said.interrupted
    assert {"type": "speaking", "utterance": 2} in link.controls


async def test_a_barge_in_mid_stream_stops_reading_the_stream() -> None:
    link, stt, tts = Loopback(ANA, "acme"), ScriptedTranscription(), Tone()
    queue: asyncio.Queue[str] = asyncio.Queue()
    pulled: list[str] = []
    closed = asyncio.Event()

    async def reply() -> AsyncIterator[str]:
        try:
            while True:
                chunk = await queue.get()
                pulled.append(chunk)
                yield chunk
        finally:
            closed.set()

    async with Session(link, transcription=stt, speech=tts) as s:
        events = s.events()
        saying = asyncio.create_task(s.say(reply(), triggered_by=HOST))
        queue.put_nowait("One two. ")
        queue.put_nowait("Three")
        await until(lambda: len(link.audio) == 2)
        stt.hear("wait", final=False, end=False)
        cut = await next_of(events, Interrupted)
        said = await saying
        queue.put_nowait(" four.")
        await asyncio.wait_for(closed.wait(), 1.0)
    assert cut.heard == said.heard == said.text == "One two. " and said.interrupted
    assert pulled == ["One two. ", "Three"] and tts.spoken == ["One two. "]

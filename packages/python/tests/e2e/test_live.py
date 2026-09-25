import asyncio
import time
from typing import Any

import pytest

from rfnry_senses import AudioFrame, Heard, Loopback, Session, TurnEnded

from ..unit.fakes import ANA, HOST, next_of

pytestmark = pytest.mark.live

PARAGRAPH = (
    "The Acme bistro opened in nineteen ninety two in a converted bakery by the river. "
    "Its terrace seats forty guests under the old plane trees, and the kitchen still bakes "
    "the bread every morning before dawn, as the bakers did a century ago."
)


async def test_a_live_model_speaks_verbatim_and_in_its_own_words(adapters: Any) -> None:
    link = Loopback(ANA, "acme")
    started = time.monotonic()
    async with Session(link, live=adapters.live()) as session:
        said = await asyncio.wait_for(session.say("Good morning.", triggered_by=HOST), 30)
        prompted = await asyncio.wait_for(
            session.prompt("Say one short sentence about the weather.", triggered_by=HOST), 30
        )
    print(
        f"\nlive: said {said.heard!r}, prompted {prompted.text!r} in {time.monotonic() - started:.1f} s"
    )
    assert said.heard.strip() and prompted.text.strip() and link.audio


async def test_the_voice_round_trips_through_speech_and_transcription(adapters: Any) -> None:
    frames: list[AudioFrame] = [
        f async for f in adapters.speech().speak("A table for two at eight.")
    ]
    silence = AudioFrame(
        data=bytes(frames[0].sample_rate // 10 * 2), sample_rate=frames[0].sample_rate
    )
    link = Loopback(ANA, "acme")
    async with Session(link, transcription=adapters.transcription()) as session:
        events = session.events()
        for frame in [*frames, *[silence] * 20]:
            await link.speak(frame)
            await asyncio.sleep(frame.duration_ms / 1000)
        heard = await next_of(events, Heard, timeout=30)
        turn = await next_of(events, TurnEnded, timeout=30)
    print(f"\nround trip: heard {heard.text!r}, turn {turn.text!r}")
    assert "table" in turn.text.lower()


async def test_an_interrupted_live_voice_reports_a_prefix_of_what_it_said(adapters: Any) -> None:
    link = Loopback(ANA, "acme", played=lambda utterance, sent: 1000)
    async with Session(link, live=adapters.live()) as session:
        saying = asyncio.create_task(session.say(PARAGRAPH, triggered_by=HOST))
        async with asyncio.timeout(30):
            while not link.audio:
                await asyncio.sleep(0.05)
        await asyncio.sleep(1)
        cut = await session.interrupt(triggered_by=HOST)
        said = await asyncio.wait_for(saying, 30)
    print(f"\nbarge-in: heard {said.heard!r}")
    assert cut is not None and said.interrupted
    assert 0 < len(said.heard) < len(PARAGRAPH) and said.heard.split()[0].lower() == "the"

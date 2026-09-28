import asyncio
from collections.abc import AsyncIterator

import pytest

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.voice import Ledger, sentences, sentences_from

from .fakes import pcm


def test_text_is_cut_into_sentences_keeping_their_spacing() -> None:
    assert sentences("Your table is ready. Shall I order the usual?") == [
        "Your table is ready. ",
        "Shall I order the usual?",
    ]
    assert sentences("Wait... what?! Fine") == ["Wait... ", "what?! ", "Fine"]
    assert sentences("v1.2 is out") == ["v1.2 is out"]


def test_heard_keeps_whole_sentences_played() -> None:
    ledger = Ledger()
    for sentence in ("One. ", "Two. "):
        ledger.sent(pcm(100), now=0.0)
        ledger.mark(sentence)
    assert ledger.heard(98) == ""
    assert ledger.heard(99) == ledger.heard(100) == "One. "
    assert ledger.heard(150) == "One. "
    assert ledger.heard(200) == ledger.heard(None) == "One. Two. "


def test_live_marks_end_with_the_audio_that_follows_them() -> None:
    ledger = Ledger()
    ledger.mark("Let me ", ahead=True)
    ledger.sent(pcm(100), now=0.0)
    ledger.mark("check. ", ahead=True)
    assert ledger.heard(100) == "Let me "
    assert ledger.heard(None) == "Let me check. "


def test_the_estimate_is_capped_by_what_was_sent() -> None:
    ledger = Ledger()
    assert ledger.estimate(5.0) == 0.0
    ledger.sent(pcm(300), now=10.0)
    assert ledger.estimate(10.15) == pytest.approx(150.0)
    assert ledger.estimate(99.0) == 300.0
    assert ledger.remaining(10.1) == pytest.approx(0.2)


async def chunks(*parts: str) -> AsyncIterator[str]:
    for part in parts:
        yield part


async def test_a_stream_is_cut_into_sentences_in_order() -> None:
    pieces = sentences_from(
        chunks("Your table", " is ready. Sh", "all I order", " the usual? ", "Fine")
    )
    assert [s async for s in pieces] == [
        "Your table is ready. ",
        "Shall I order the usual? ",
        "Fine",
    ]


async def test_a_sentence_is_ready_as_soon_as_text_follows_it() -> None:
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def feed() -> AsyncIterator[str]:
        while (chunk := await queue.get()) is not None:
            yield chunk

    pieces = sentences_from(feed())
    queue.put_nowait("One two. Th")
    assert await anext(pieces) == "One two. "
    queue.put_nowait("ree.")
    queue.put_nowait(None)
    assert [s async for s in pieces] == ["Three."]


def test_nothing_is_heard_when_nothing_played() -> None:
    ledger = Ledger()
    ledger.mark("The terrace is open. ", ahead=True)
    ledger.sent(AudioFrame(data=bytes(2), sample_rate=24000), 0.0)
    ledger.sent(AudioFrame(data=bytes(4800), sample_rate=24000), 0.0)
    assert ledger.heard(0) == ""
    assert ledger.heard(100) == "The terrace is open. "

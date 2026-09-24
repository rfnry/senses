from rfnry_senses.models import Image
from rfnry_senses.transport import Loopback, Played, Transport

from .fakes import ANA, JPEG, pcm


async def test_the_loopback_is_a_transport_that_answers_stop_with_what_it_received() -> None:
    link = Loopback(ANA, "acme")
    assert isinstance(link, Transport)
    await link.send_audio(pcm(100), 1)
    await link.send_audio(pcm(100), 1)
    await link.send_control({"type": "stop", "utterance": 1})
    await link.show(JPEG)
    await link.hang_up()
    received = [item async for item in link.inbound()]
    assert received[0] == Played(utterance=1, ms=200)
    assert isinstance(received[1], Image) and received[1].media_type == "image/jpeg"


async def test_the_loopback_plays_what_it_is_told_or_stays_silent() -> None:
    told = Loopback(ANA, "acme", played=lambda utterance, sent: 70)
    await told.send_control({"type": "silent", "utterance": 4})
    silent = Loopback(ANA, "acme", answer=False)
    await silent.send_control({"type": "stop", "utterance": 4})
    await told.hang_up()
    await silent.hang_up()
    assert [item async for item in told.inbound()] == [Played(utterance=4, ms=70)]
    assert [item async for item in silent.inbound()] == []

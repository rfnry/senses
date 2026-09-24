import asyncio
import re

import pytest

from rfnry_senses.errors import ConfigurationError, InputError, NotFoundError
from rfnry_senses.events import Looked
from rfnry_senses.models import Image, Text
from rfnry_senses.session import Session
from rfnry_senses.transport import Loopback

from .conftest import Clock
from .fakes import ANA, HOST, JPEG, ScriptedConnection, ScriptedLive, next_of, until


async def test_look_returns_the_newest_frames_as_parts(clock: Clock) -> None:
    link = Loopback(ANA, "acme")
    async with Session(link, video=True) as s:
        events = s.events()
        for n in range(3):
            await link.show(JPEG + bytes([n]))
            clock.advance(1)
        await asyncio.sleep(0.01)
        parts = await s.look(frames=2, triggered_by=HOST)
        looked = await next_of(events, Looked)
    assert [type(p) for p in parts] == [Image, Text, Image, Text]
    assert (
        isinstance(parts[0], Image)
        and parts[0].data == JPEG + b"\x01"
        and parts[0].media_type == "image/jpeg"
    )
    assert isinstance(parts[1], Text) and re.fullmatch(
        r"frame at \d\d:\d\d:\d\d\.\d{3}", parts[1].text
    )
    assert looked.frames == 2 and looked.triggered_by == HOST
    assert any(c["type"] == "event" and c["event"]["kind"] == "looked" for c in link.controls)


async def test_a_stalled_camera_leaves_an_empty_window(clock: Clock) -> None:
    link = Loopback(ANA, "acme")
    async with Session(link, video=True) as s:
        await link.show(JPEG)
        await asyncio.sleep(0.01)
        assert len(await s.look(triggered_by=HOST)) == 2
        clock.advance(10.5)
        with pytest.raises(NotFoundError, match="^session: no frame in the last 10 s$"):
            await s.look(triggered_by=HOST)
        with pytest.raises(InputError, match="^session: frames must be at least 1$"):
            await s.look(frames=0, triggered_by=HOST)


async def test_without_video_frames_are_dropped_and_look_is_refused() -> None:
    link = Loopback(ANA, "acme")
    async with Session(link) as s:
        await link.show(JPEG)
        with pytest.raises(ConfigurationError, match="^session: look needs video=True$"):
            await s.look(triggered_by=HOST)


class Slow(ScriptedConnection):
    def __init__(self) -> None:
        super().__init__()
        self.gate = asyncio.Event()

    async def send_image(self, image: Image) -> None:
        self.images.append(image)
        await self.gate.wait()


async def test_a_live_model_gets_the_newest_frame_when_it_falls_behind() -> None:
    live, link, slow = ScriptedLive(), Loopback(ANA, "acme"), Slow()
    live.connection = slow
    async with Session(link, live=live, video=True):
        await link.show(JPEG + b"\x00")
        await until(lambda: len(slow.images) == 1)
        for n in range(1, 4):
            await link.show(JPEG + bytes([n]))
        await asyncio.sleep(0.01)
        slow.gate.set()
        await until(lambda: len(slow.images) == 2)
    assert [i.data for i in slow.images] == [JPEG + b"\x00", JPEG + b"\x03"]

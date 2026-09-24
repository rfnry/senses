import asyncio
import fractions
import json
from typing import Any

import pytest

pytest.importorskip("aiortc")

import av
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack

from rfnry_senses.errors import InputError
from rfnry_senses.events import Closed
from rfnry_senses.models import Image
from rfnry_senses.session import Session
from rfnry_senses.webrtc import accept_offer

from ..unit.fakes import ANA, HOST, ScriptedTranscription, Tone, next_of, until

pytestmark = pytest.mark.webrtc


class Silence(MediaStreamTrack):
    kind = "audio"

    def __init__(self) -> None:
        super().__init__()
        self.pts = 0

    async def recv(self) -> av.AudioFrame:
        await asyncio.sleep(0.02)
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.planes[0].update(bytes(1920))
        frame.sample_rate = 48000
        frame.pts = self.pts
        frame.time_base = fractions.Fraction(1, 48000)
        self.pts += 960
        return frame


class Client:
    def __init__(self, protocol: int = 1) -> None:
        self.pc = RTCPeerConnection()
        self.messages: list[dict[str, Any]] = []
        self.audio: list[Any] = []
        self.pc.addTrack(Silence())
        self.pc.addTrack(VideoStreamTrack())
        self.channel = self.pc.createDataChannel("senses")

        @self.channel.on("open")
        def on_open() -> None:
            self.channel.send(json.dumps({"type": "hello", "protocol": protocol}))

        @self.channel.on("message")
        def on_message(message: str) -> None:
            control = json.loads(message)
            self.messages.append(control)
            if control["type"] in ("stop", "silent"):
                ms = 0 if control["type"] == "stop" else 10_000
                self.channel.send(
                    json.dumps({"type": "played", "utterance": control["utterance"], "ms": ms})
                )

        @self.pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            asyncio.ensure_future(self._collect(track))

    async def _collect(self, track: MediaStreamTrack) -> None:
        while True:
            try:
                self.audio.append(await track.recv())
            except Exception:
                return

    async def offer(self) -> str:
        await self.pc.setLocalDescription(await self.pc.createOffer())
        return str(self.pc.localDescription.sdp)


async def test_a_cascaded_session_over_a_peer_connection() -> None:
    client = Client()
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    assert link.scope == "acme" and link.identity == ANA
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    stt, tts = ScriptedTranscription(), Tone()
    try:
        async with Session(link, transcription=stt, speech=tts, video=True) as s:
            await until(lambda: len(stt.fed) > 10, timeout=10)
            assert all(frame.sample_rate == 16000 and frame.channels == 1 for frame in stt.fed)
            await until(lambda: s._window is not None and bool(s._window.newest(1)), timeout=10)
            parts = await s.look(triggered_by=HOST)
            said = await s.say("Hello there, how are you today?", triggered_by=HOST)
            await until(lambda: len(client.audio) > 5, timeout=5)
        assert isinstance(parts[0], Image) and (parts[0].data or b"")[:2] == b"\xff\xd8"
        assert (
            said.heard == "Hello there, how are you today?" and {"type": "ready"} in client.messages
        )
    finally:
        await client.pc.close()


async def test_a_wrong_protocol_on_the_channel_ends_the_transport() -> None:
    client = Client(protocol=2)
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    try:
        async with Session(link) as s:
            events = s.events()
            closed = await next_of(events, Closed, timeout=10)
        assert closed.reason == "hung_up"
        await until(lambda: any(m["type"] == "error" for m in client.messages), timeout=5)
        error = next(m for m in client.messages if m["type"] == "error")
        assert error["message"] == "transport: protocol 2 is not supported; this server speaks 1"
    finally:
        await client.pc.close()


async def test_an_empty_scope_is_the_consumers_bug() -> None:
    client = Client()
    try:
        with pytest.raises(InputError, match="^transport: scope is empty$"):
            await accept_offer(await client.offer(), identity=ANA, scope="")
    finally:
        await client.pc.close()

import asyncio
import fractions
import json
import struct
import time
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("aiortc")

import av
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription, VideoStreamTrack

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError
from rfnry_senses.events import Closed
from rfnry_senses.models import Image
from rfnry_senses.session import Session
from rfnry_senses.webrtc import _Marker, _Voice, accept_offer

from ..unit.fakes import ANA, HOST, ScriptedTranscription, Tone, lookalike, next_of, until

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
    def __init__(
        self, protocol: int = 1, played: Callable[[dict[str, Any]], int] | None = None
    ) -> None:
        self.played = played
        self.stamped: list[tuple[float, dict[str, Any]]] = []
        self.arrivals: list[float] = []
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
            self.stamped.append((time.monotonic(), control))
            if control["type"] in ("stop", "silent"):
                ms = 0 if control["type"] == "stop" else 10_000
                if self.played is not None and control["type"] == "silent":
                    ms = self.played(control)
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
                self.arrivals.append(time.monotonic())
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


async def test_accept_offer_reads_the_person_by_their_fields() -> None:
    client = Client()
    try:
        link = await accept_offer(await client.offer(), identity=lookalike(ANA), scope="acme")
        assert link.identity == ANA
        await link.close()
    finally:
        await client.pc.close()


async def test_a_cut_fades_the_voice_out_over_the_next_sixty_milliseconds() -> None:
    voice = _Voice()
    loud = (16000).to_bytes(2, "little", signed=True)
    voice.push(AudioFrame(data=loud * 24000, sample_rate=24000))
    voice.clear()
    frames = [await voice.recv() for _ in range(3)]
    assert voice._queue.empty()
    levels = [
        abs(int.from_bytes(bytes(f.planes[0])[i : i + 2], "little", signed=True))
        for f in frames
        for i in (0, 1918)
    ]
    assert levels == sorted(levels, reverse=True)
    assert levels[0] == 16000 and levels[-1] < 600


def tone(ms: int, value: int, rate: int) -> AudioFrame:
    return AudioFrame(data=struct.pack("<h", value) * (rate * ms // 1000), sample_rate=rate)


def values(frame: av.AudioFrame) -> set[int]:
    data = bytes(frame.planes[0])[: frame.samples * 2]
    return set(struct.unpack(f"<{frame.samples}h", data))


class Speech:
    def __init__(self, *rates: int, ms: int = 200, words: bool = True) -> None:
        self.words = words
        self.rates = list(rates)
        self.ms = ms

    async def speak(self, text: str):  # type: ignore[no-untyped-def]
        rate = self.rates.pop(0) if len(self.rates) > 1 else self.rates[0]
        for _ in text.split() if self.words else [text]:
            yield tone(self.ms, 100, rate)


async def test_an_utterance_not_a_whole_number_of_frames_is_sent_whole() -> None:
    voice = _Voice()
    voice.push(tone(30, 1111, 24000))
    voice.finish()
    voice.push(tone(40, 2222, 24000))
    voice.finish()
    frames = [await voice.recv() for _ in range(4)]
    assert voice._queue.empty()
    assert all(values(f) <= {0, 1111} for f in frames[:2])
    assert all(values(f) <= {0, 2222} for f in frames[2:])

    sent = [0.0]
    client = Client(played=lambda _: round(sent[0]))
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    original = link._voice.recv

    async def counted() -> av.AudioFrame:
        out = await original()
        sent[0] += out.samples / out.sample_rate * 1000
        return out

    link._voice.recv = counted  # type: ignore[method-assign]
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    try:
        async with Session(link, speech=Speech(24000, ms=905, words=False)) as s:
            said = await s.say("The table is ready. Please come in.", triggered_by=HOST)
        assert round(sent[0]) == 1820
        assert said.heard == "The table is ready. Please come in."
    finally:
        await client.pc.close()


async def test_a_change_of_rate_keeps_the_voice() -> None:
    client = Client()
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    try:
        async with Session(link, speech=Speech(24000, 16000, 24000, ms=200)) as s:
            await until(lambda: {"type": "ready"} in client.messages, timeout=10)

            async def heard_more() -> None:
                target = len(client.audio) + 30
                await s.say("one two three four five", triggered_by=HOST)
                await until(lambda: len(client.audio) > target, timeout=5)

            for _ in range(3):
                await heard_more()
    finally:
        await client.pc.close()


async def test_speaking_and_silent_leave_with_their_audio() -> None:
    client = Client()
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    left = [0]
    delivered: list[tuple[str, int, int]] = []
    original = link._voice.recv
    deliver = link._deliver

    async def counted() -> av.AudioFrame:
        out = await original()
        left[0] += 1
        return out

    def recorded(text: str) -> None:
        frame = json.loads(text)
        delivered.append((frame["type"], frame["utterance"], left[0]))
        deliver(text)

    link._voice.recv = counted  # type: ignore[method-assign]
    link._voice._emit = recorded
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    try:
        async with Session(link, speech=Speech(24000, ms=500)) as s:
            await until(lambda: {"type": "ready"} in client.messages, timeout=10)
            await s.say("one two three four", triggered_by=HOST)
            await s.say("one two three four", triggered_by=HOST)
            await asyncio.sleep(0.3)
        kinds = [(kind, number) for kind, number, _ in delivered]
        assert kinds == [("speaking", 1), ("silent", 1), ("speaking", 2), ("silent", 2)]
        counts = [count for _, _, count in delivered]
        assert counts[0] == 0 and counts[1] == counts[2] and counts[1] in (100, 101)
        assert counts[3] == left[0] and counts[3] - counts[1] in (100, 101)
    finally:
        await client.pc.close()


def kinds_queued(voice: _Voice) -> list[str]:
    out = []
    while not voice._queue.empty():
        item = voice._queue.get_nowait()
        if isinstance(item, _Marker):
            frame = json.loads(item.text or "{}")
            out.append(f"{frame['type']} {frame['utterance']}")
        else:
            out.append("frame")
    return out


async def test_overlapping_live_utterances_each_keep_their_own_markers() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    try:
        await link.send_control({"type": "speaking", "utterance": 1})
        await link.send_audio(tone(50, 2222, 24000), 1)
        await link.send_control({"type": "speaking", "utterance": 2})
        await link.send_audio(tone(30, 2222, 24000), 2)
        await link.send_control({"type": "silent", "utterance": 1})
        await link.send_audio(tone(30, 2222, 24000), 2)
        await link.send_control({"type": "silent", "utterance": 2})
        queued = []
        while not link._voice._queue.empty():
            queued.append(link._voice._queue.get_nowait())
        labels = [
            f"{json.loads(i.text or '')['type']} {json.loads(i.text or '')['utterance']}"
            if isinstance(i, _Marker)
            else "frame"
            for i in queued
        ]
        assert labels == [
            "speaking 1",
            *["frame"] * 3,
            "silent 1",
            "speaking 2",
            *["frame"] * 3,
            "silent 2",
        ]
        start = labels.index("speaking 2")
        inside = [i for i in queued[start:] if isinstance(i, AudioFrame)]
        samples = [
            struct.unpack("<h", f.data[k : k + 2])[0]
            for f in inside
            for k in range(0, len(f.data), 2)
        ]
        assert samples.count(0) == 0
    finally:
        await link.close()


async def test_a_cut_keeps_the_fade_and_drops_the_markers_after_it() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    try:
        await link.send_control({"type": "speaking", "utterance": 1})
        await link.send_audio(tone(200, 100, 24000), 1)
        await link.send_control({"type": "speaking", "utterance": 2})
        await link.send_audio(tone(100, 100, 24000), 2)
        link.clear_audio()
        assert kinds_queued(link._voice) == ["frame", "frame", "frame"]
        assert link._unsent == []
    finally:
        await link.close()


def answers(link: Any, stop: int) -> list[int]:
    ours = [link._inbox.get_nowait().utterance for _ in range(link._inbox.qsize())]
    delivered = [json.loads(t)["utterance"] for t in link._unsent if '"speaking"' in t]
    browser = [n for n in {*delivered, stop} if n not in link._cut]
    return sorted(ours + browser)


async def test_a_cut_answers_the_utterances_whose_voice_never_left_at_zero() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    try:
        for number in (1, 2, 3):
            await link.send_control({"type": "speaking", "utterance": number})
            await link.send_audio(tone(200, 100, 24000), number)
        link.clear_audio()
        assert link._unsent == []
        assert kinds_queued(link._voice) == ["frame", "frame", "frame"]
        assert answers(link, 3) == [1, 2, 3]
    finally:
        await link.close()


async def test_a_cut_answers_a_speaking_kept_among_the_fade_frames() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    try:
        await link.send_control({"type": "speaking", "utterance": 1})
        await link.send_audio(tone(60, 100, 24000), 1)
        await link.send_control({"type": "speaking", "utterance": 2})
        await link.send_audio(tone(200, 100, 24000), 2)
        await link._voice.recv()
        link.clear_audio()
        assert [json.loads(t)["utterance"] for t in link._unsent] == [1]
        assert answers(link, 2) == [1, 2]
    finally:
        await link.close()


async def test_a_cut_answers_a_speaking_held_while_its_frame_waits() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    try:
        await link.send_control({"type": "speaking", "utterance": 1})
        await link.send_audio(tone(20, 100, 24000), 1)
        await link.send_control({"type": "silent", "utterance": 1})
        await link._voice.recv()
        await link.send_control({"type": "speaking", "utterance": 2})
        await link.send_audio(tone(200, 100, 24000), 2)
        await link.send_control({"type": "speaking", "utterance": 3})
        await link.send_audio(tone(200, 100, 24000), 3)
        link._voice._start, link._voice._samples = time.monotonic() + 0.5, 0
        waiting = asyncio.ensure_future(link._voice.recv())
        await asyncio.sleep(0.05)
        assert not waiting.done()
        link.clear_audio()
        waiting.cancel()
        assert [json.loads(t)["utterance"] for t in link._unsent if '"speaking"' in t] == [1]
        assert answers(link, 3) == [1, 2, 3]
    finally:
        await link.close()


async def test_an_empty_frame_sends_nothing_and_does_not_raise() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    try:
        await link.send_audio(AudioFrame(data=b"", sample_rate=24000), 1)
        assert link._voice._queue.empty()
    finally:
        await link.close()


async def test_a_refused_hello_sends_no_ready() -> None:
    client = Client(protocol=2)
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    try:
        async with Session(link) as s:
            await next_of(s.events(), Closed, timeout=10)
        await asyncio.sleep(0.3)
        assert [m["type"] for m in client.messages] == ["error"]
    finally:
        await client.pc.close()

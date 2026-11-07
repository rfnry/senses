import asyncio
import fractions
import itertools
import json
import math
import struct
import time
from collections.abc import Callable
from typing import Any

import pytest

pytest.importorskip("aiortc")

import av
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
    VideoStreamTrack,
)
from aiortc.codecs.opus import OpusEncoder

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError, TransportError
from rfnry_senses.events import Closed
from rfnry_senses.models import Image
from rfnry_senses.session import Session
from rfnry_senses.webrtc import _Encoded, _Marker, _Voice, accept_offer

from ..unit.fakes import (
    ANA,
    HOST,
    ScriptedLive,
    ScriptedTranscription,
    Tone,
    lookalike,
    next_of,
    until,
)

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
        self,
        protocol: int = 3,
        played: Callable[[dict[str, Any]], int] | None = None,
        codecs: tuple[str, ...] = (),
        greets: bool = True,
    ) -> None:
        self.played = played
        self.opened = False
        self.stamped: list[tuple[float, dict[str, Any]]] = []
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.messages: list[dict[str, Any]] = []
        self.audio: list[Any] = []
        self.pc.addTrack(Silence())
        self.pc.addTrack(VideoStreamTrack())
        if codecs:
            offered = RTCRtpSender.getCapabilities("audio").codecs
            self.pc.getTransceivers()[0].setCodecPreferences(
                [codec for name in codecs for codec in offered if codec.name.lower() == name]
            )
        self.channel = self.pc.createDataChannel("senses")

        @self.channel.on("open")
        def on_open() -> None:
            self.opened = True
            if greets:
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
    client = Client(protocol=1)
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    try:
        async with Session(link) as s:
            events = s.events()
            closed = await next_of(events, Closed, timeout=10)
        assert closed.reason == "hung_up"
        await until(lambda: any(m["type"] == "error" for m in client.messages), timeout=5)
        error = next(m for m in client.messages if m["type"] == "error")
        assert error["message"] == "transport: protocol 1 is not supported; this server speaks 3"
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
            await until(lambda: {"type": "ready"} in client.messages, timeout=10)
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
    delivered: list[tuple[str, int, int, int]] = []
    original = link._voice.recv
    deliver = link._deliver

    async def counted() -> av.AudioFrame:
        out = await original()
        left[0] += 1
        return out

    def recorded(text: str) -> None:
        frame = json.loads(text)
        delivered.append((frame["type"], frame["utterance"], left[0], frame["sent_ms"]))
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
        kinds = [(kind, number) for kind, number, _, _ in delivered]
        assert kinds == [("speaking", 1), ("silent", 1), ("speaking", 2), ("silent", 2)]
        counts = [count for _, _, count, _ in delivered]
        assert counts[0] == 0 and counts[1] == counts[2] and counts[1] in (100, 101)
        assert counts[3] == left[0] and counts[3] - counts[1] in (100, 101)
        stamps = [sent_ms for _, _, _, sent_ms in delivered]
        assert stamps == [count * 20 for count in counts]
    finally:
        await client.pc.close()


def stamp(frame: av.AudioFrame) -> int:
    assert frame.pts is not None
    return frame.pts


def sine(ms: int, hz: int, rate: int) -> AudioFrame:
    count = rate * ms // 1000
    samples = (int(12000 * math.sin(2 * math.pi * hz * i / rate)) for i in range(count))
    return AudioFrame(data=struct.pack(f"<{count}h", *samples), sample_rate=rate)


def tone_share(frames: list[Any], hz: int) -> float:
    heard: list[int] = []
    for frame in frames:
        channels = len(frame.layout.channels)
        data = bytes(frame.planes[0])[: frame.samples * channels * 2]
        heard += struct.unpack(f"<{len(data) // 2}h", data)[::channels]
    turn = 2 * math.pi * hz / frames[0].sample_rate
    real = sum(sample * math.cos(turn * i) for i, sample in enumerate(heard))
    imaginary = sum(sample * math.sin(turn * i) for i, sample in enumerate(heard))
    return 2 * (real**2 + imaginary**2) / len(heard) / sum(sample**2 for sample in heard)


async def tone_heard_by(client: Client, answer_of: Any) -> float:
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=answer_of.answer, type="answer"))
    await until(lambda: client.channel.readyState == "open", timeout=10)
    answer_of._voice.push(sine(1000, 1000, 48000))
    answer_of._voice.finish()
    await until(lambda: len(client.audio) >= 45, timeout=10)
    return tone_share(client.audio, 1000)


async def test_a_peer_offering_opus_first_hears_the_voice_clean() -> None:
    client = Client()
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    try:
        assert await tone_heard_by(client, link) > 0.9
    finally:
        await client.pc.close()
        await link.close()


async def test_an_offer_with_opus_behind_another_codec_gets_a_clean_opus_voice() -> None:
    client = Client(codecs=("pcmu", "opus"))
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    try:
        assert await tone_heard_by(client, link) > 0.9
        assert "opus/48000" in link.answer
        assert "PCMU" not in link.answer
    finally:
        await client.pc.close()
        await link.close()


@pytest.mark.parametrize("codec", ["pcmu", "pcma", "g722"])
async def test_an_offer_without_opus_is_refused(codec: str) -> None:
    client = Client(codecs=(codec,))
    try:
        with pytest.raises(TransportError) as refused:
            await accept_offer(await client.offer(), identity=ANA, scope="acme")
        assert str(refused.value) == "webrtc: the offer has no opus audio"
    finally:
        await client.pc.close()


async def test_an_offer_with_no_audio_is_refused_the_same_way() -> None:
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    pc.createDataChannel("senses")
    await pc.setLocalDescription(await pc.createOffer())
    try:
        with pytest.raises(TransportError) as refused:
            await accept_offer(pc.localDescription.sdp, identity=ANA, scope="acme")
        assert str(refused.value) == "webrtc: the offer has no opus audio"
    finally:
        await pc.close()


async def spoken(voice: _Voice, ms: int) -> list[int]:
    voice.push(tone(ms, 2000, voice.RATE))
    voice.finish()
    return [stamp(await voice.recv()) for _ in range(ms // voice.FRAME_MS)]


async def test_the_voice_timestamps_keep_time_across_a_pause() -> None:
    voice = _Voice()
    before = await spoken(voice, 200)
    left = time.monotonic()
    await asyncio.sleep(1.0)
    voice.push(tone(200, 2000, voice.RATE))
    voice.finish()
    first = stamp(await voice.recv())
    paused = time.monotonic() - left
    rest = [stamp(await voice.recv()) for _ in range(9)]
    advanced = (first - before[-1]) / voice.RATE
    assert advanced == pytest.approx(paused - voice.PREBUFFER_S, abs=0.05)
    assert rest == [first + 960 * (i + 1) for i in range(9)]


async def test_every_packet_after_a_pause_carries_the_silence() -> None:
    voice = _Voice()
    track = _Encoded(voice)
    pack = OpusEncoder().pack
    voice.push(tone(100, 2000, voice.RATE))
    voice.finish()
    before = [pack(await track.recv())[1] for _ in range(5)]
    await asyncio.sleep(0.5)
    voice.push(tone(100, 2000, voice.RATE))
    voice.finish()
    after = [pack(await track.recv())[1] for _ in range(5)]
    assert after[0] - before[-1] > voice.RATE // 4
    assert [later - earlier for earlier, later in itertools.pairwise(before)] == [960] * 4
    assert [later - earlier for earlier, later in itertools.pairwise(after)] == [960] * 4


async def test_a_peer_receives_the_silence_on_the_first_packet_after_a_pause() -> None:
    client = Client()
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    try:
        async with Session(link, speech=Speech(48000)) as s:
            await until(lambda: {"type": "ready"} in client.messages, timeout=10)
            await s.say("one two", triggered_by=HOST)
            await asyncio.sleep(0.5)
            await s.say("one two", triggered_by=HOST)
            await asyncio.sleep(0.3)
        pts = [frame.pts for frame in client.audio]
        steps = [later - earlier for earlier, later in itertools.pairwise(pts)]
        assert steps.index(max(steps)) == 19
        assert max(steps) > 9600
        assert steps[:19] == [960] * 19
    finally:
        await client.pc.close()


async def test_a_short_gap_leaves_the_voice_timestamps_running() -> None:
    voice = _Voice()
    before = await spoken(voice, 100)
    await asyncio.sleep(0.05)
    after = await spoken(voice, 100)
    assert after[0] == before[-1] + 960


async def test_cuts_leave_the_voice_timestamps_on_the_clock() -> None:
    voice = _Voice()
    started = time.monotonic()
    stamps: list[int] = []
    for _ in range(4):
        voice.push(tone(200, 2000, voice.RATE))
        voice.finish()
        stamps += [stamp(await voice.recv()) for _ in range(8)]
        voice.clear()
        while not voice._queue.empty():
            stamps.append(stamp(await voice.recv()))
        await asyncio.sleep(0.3)
    voice.push(tone(200, 2000, voice.RATE))
    voice.finish()
    last = stamp(await voice.recv())
    assert stamps == sorted(set(stamps))
    assert last > stamps[-1]
    assert last / voice.RATE == pytest.approx(time.monotonic() - started, abs=0.06)


def kinds_queued(voice: _Voice) -> list[str]:
    out = []
    while not voice._queue.empty():
        item = voice._queue.get_nowait()
        if isinstance(item, _Marker):
            frame = item.frame or {}
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
            f"{i.frame['type']} {i.frame['utterance']}"
            if isinstance(i, _Marker) and i.frame
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
        await link.send_control({"type": "speaking", "utterance": 3})
        await link.send_audio(tone(200, 100, 24000), 3)
        await link._voice.recv()
        link.clear_audio()
        assert [json.loads(t)["utterance"] for t in link._unsent] == [1]
        assert answers(link, 3) == [1, 2, 3]
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


async def test_a_silent_filled_after_its_marker_left_keeps_the_stamp_of_its_last_frame() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    rate = link._voice.RATE
    try:
        await link.send_control({"type": "speaking", "utterance": 1})
        await link.send_audio(tone(60, 100, rate), 1)
        await link.send_control({"type": "speaking", "utterance": 2})
        await link.send_audio(tone(60, 100, rate), 2)
        for _ in range(5):
            await link._voice.recv()
        await link.send_control({"type": "silent", "utterance": 1})
        assert link._voice.sent_ms == 100
        assert json.loads(link._unsent[-1]) == {"type": "silent", "utterance": 1, "sent_ms": 60}
    finally:
        await link.close()


async def test_a_stop_carries_what_the_voice_has_sent() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    rate = link._voice.RATE
    try:
        await link.send_control({"type": "speaking", "utterance": 1})
        await link.send_audio(tone(100, 100, rate), 1)
        for _ in range(3):
            await link._voice.recv()
        await link.send_control({"type": "stop", "utterance": 1})
        assert json.loads(link._unsent[-1]) == {"type": "stop", "utterance": 1, "sent_ms": 60}
    finally:
        await link.close()


async def test_a_silent_for_an_utterance_a_cut_took_still_carries_a_stamp() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    rate = link._voice.RATE
    try:
        await link.send_control({"type": "speaking", "utterance": 1})
        await link.send_audio(tone(100, 100, rate), 1)
        await link._voice.recv()
        link.clear_audio()
        await link.send_control({"type": "speaking", "utterance": 2})
        await link.send_audio(tone(100, 100, rate), 2)
        await link.send_control({"type": "silent", "utterance": 1})
        assert link._voice.sent_ms == 20
        assert json.loads(link._unsent[-1]) == {
            "type": "silent",
            "utterance": 1,
            "sent_ms": link._voice.sent_ms,
        }
    finally:
        await link.close()


async def test_the_fade_counts_toward_the_next_speaking() -> None:
    link = await accept_offer(await Client().offer(), identity=ANA, scope="acme")
    rate = link._voice.RATE
    emitted: list[dict[str, Any]] = []
    link._voice._emit = lambda text: emitted.append(json.loads(text))
    try:
        await link.send_control({"type": "speaking", "utterance": 1})
        await link.send_audio(tone(200, 100, rate), 1)
        link.clear_audio()
        faded = link._voice._queue.qsize()
        await link.send_control({"type": "speaking", "utterance": 2})
        await link.send_audio(tone(100, 100, rate), 2)
        for _ in range(faded + 5):
            await link._voice.recv()
        assert faded == 3
        assert emitted == [{"type": "speaking", "utterance": 2, "sent_ms": 20 * faded}]
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
    client = Client(protocol=1)
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    try:
        async with Session(link) as s:
            await next_of(s.events(), Closed, timeout=10)
        await asyncio.sleep(0.3)
        assert [m["type"] for m in client.messages] == ["error"]
    finally:
        await client.pc.close()


async def greeted(link: Any, client: Client) -> bool:
    try:
        return bool(await asyncio.wait_for(link.greeted(), 10))
    except TimeoutError:
        pytest.fail(
            f"no hello in 10 s: client {client.pc.connectionState}/{client.pc.iceConnectionState}, "
            f"channel {client.channel.readyState}, opened {client.opened}, "
            f"server {link._pc.connectionState}/{link._pc.iceConnectionState}"
        )


async def answered(client: Client) -> Any:
    link = await accept_offer(await client.offer(), identity=ANA, scope="acme")
    await client.pc.setRemoteDescription(RTCSessionDescription(sdp=link.answer, type="answer"))
    return link


async def test_greeted_is_true_once_the_hello_is_accepted() -> None:
    client = Client()
    link = await answered(client)
    try:
        assert await greeted(link, client) is True
        async with Session(link) as s:
            await until(lambda: {"type": "ready"} in client.messages, timeout=5)
            await s.close()
    finally:
        await client.pc.close()


async def test_greeted_is_false_for_a_refused_hello_and_the_client_is_told_why() -> None:
    client = Client(protocol=1)
    link = await answered(client)
    try:
        assert await greeted(link, client) is False
        await until(lambda: any(m["type"] == "error" for m in client.messages), timeout=5)
        assert [m["type"] for m in client.messages] == ["error"]
        assert [item async for item in link.inbound()] == []
    finally:
        await client.pc.close()


async def test_greeted_waits_for_a_peer_that_never_greets_until_the_peer_leaves() -> None:
    client = Client(greets=False)
    link = await answered(client)
    waiting = asyncio.create_task(link.greeted())
    try:
        await until(lambda: client.opened, timeout=10)
        await asyncio.sleep(0.1)
        assert not waiting.done()
    finally:
        await client.pc.close()
    assert await asyncio.wait_for(waiting, 45) is False


async def test_greeted_is_false_once_the_transport_ends_first() -> None:
    client = Client(greets=False)
    link = await answered(client)
    waiting = asyncio.create_task(link.greeted())
    try:
        await until(lambda: client.opened, timeout=10)
        await link.close()
        assert await asyncio.wait_for(waiting, 1) is False
        assert await asyncio.wait_for(link.greeted(), 1) is False
    finally:
        await client.pc.close()


async def test_a_refused_hello_opens_no_live_model_when_greeted_comes_first() -> None:
    client, live = Client(protocol=1), ScriptedLive()
    link = await answered(client)
    try:
        if await greeted(link, client):
            async with Session(link, live=live):
                pass
        assert live.opened == {}
    finally:
        await client.pc.close()


async def frames_of(pieces: list[AudioFrame]) -> list[av.AudioFrame]:
    voice = _Voice()
    for piece in pieces:
        voice.push(piece)
    voice.finish()
    return [await voice.recv() for _ in range(voice._queue.qsize())]


def stereo(ms: int, value: int, rate: int) -> AudioFrame:
    data = struct.pack("<hh", value, value) * (rate * ms // 1000)
    return AudioFrame(data=data, sample_rate=rate, channels=2)


@pytest.mark.parametrize(
    "pieces",
    [
        [stereo(905, 4000, 48000), stereo(905, 4000, 48000)],
        [tone(905, 4000, 24000), tone(905, 4000, 48000)],
        [tone(905, 4000, 16000), stereo(905, 4000, 16000)],
    ],
    ids=["stereo", "a rate change", "a channel change"],
)
async def test_the_voice_keeps_the_length_of_what_it_was_given(pieces: list[AudioFrame]) -> None:
    frames = await frames_of(pieces)
    assert all(f.sample_rate == 48000 and f.samples == 960 for f in frames)
    assert 1810 <= len(frames) * 20 <= 1840

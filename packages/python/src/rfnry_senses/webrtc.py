import array
import asyncio
import contextlib
import fractions
import logging
import sys
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from typing import Any

import av
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCDataChannel,
    RTCIceServer,
    RTCPeerConnection,
    RTCRtpSender,
    RTCSessionDescription,
)
from aiortc.codecs.opus import OpusEncoder
from aiortc.mediastreams import MediaStreamError

from rfnry_senses.contracts import AudioFrame
from rfnry_senses.errors import InputError, TransportError
from rfnry_senses.models import Identity, IdentityLike, Image, as_identity
from rfnry_senses.transport import Played
from rfnry_senses.websocket import check_hello
from rfnry_senses.wire import control, parse_played

__all__ = ["WebRTCTransport", "accept_offer"]

logger = logging.getLogger(__name__)


def fade(frame: AudioFrame, begin: float, end: float) -> AudioFrame:
    samples = array.array("h", frame.data)
    if sys.byteorder == "big":
        samples.byteswap()
    count = len(samples) // frame.channels
    for i in range(len(samples)):
        position = i // frame.channels
        samples[i] = int(samples[i] * (begin + (end - begin) * position / count))
    if sys.byteorder == "big":
        samples.byteswap()
    return AudioFrame(
        data=samples.tobytes(), sample_rate=frame.sample_rate, channels=frame.channels
    )


class _Marker:
    def __init__(self, frame: dict[str, Any] | None, with_next: bool) -> None:
        self.frame = frame
        self.with_next = with_next
        self.sent_ms: int | None = None


class _Voice(MediaStreamTrack):
    kind = "audio"
    RATE = 48000
    FRAME_MS = 20
    CHUNK = RATE * FRAME_MS // 1000 * 2
    PREBUFFER_S = 0.12
    GAP_S = 0.1
    FADE_FRAMES = 3

    def __init__(self, emit: Callable[[str], None] | None = None) -> None:
        super().__init__()
        self._emit = emit
        self._queue: asyncio.Queue[AudioFrame | _Marker] = asyncio.Queue()
        self._rest = b""
        self._pts = 0
        self._began: float | None = None
        self._start: float | None = None
        self._samples = 0
        self.sent_ms = 0
        self._resampler: av.AudioResampler | None = None
        self._source: tuple[int, int] | None = None
        self._held: list[dict[str, Any]] = []

    def push(self, frame: AudioFrame) -> None:
        if not frame.data:
            return
        source = (frame.sample_rate, frame.channels)
        if self._source != source:
            self._drain()
            self._source = source
            self._resampler = av.AudioResampler(format="s16", layout="mono", rate=self.RATE)
        incoming = av.AudioFrame(
            format="s16",
            layout="mono" if frame.channels == 1 else "stereo",
            samples=len(frame.data) // (2 * frame.channels),
        )
        incoming.planes[0].update(frame.data)
        incoming.sample_rate = frame.sample_rate
        if self._resampler is not None:
            self._take(self._resampler.resample(incoming))

    def finish(self) -> None:
        self._drain()
        if self._rest:
            self._rest += bytes(self.CHUNK - len(self._rest))
            self._take([])

    def mark(self, frame: dict[str, Any] | None, *, with_next: bool) -> _Marker:
        marker = _Marker(frame, with_next)
        self._queue.put_nowait(marker)
        return marker

    def fill(self, marker: _Marker, frame: dict[str, Any]) -> None:
        if marker.sent_ms is None:
            marker.frame = frame
        else:
            self._fire([{**frame, "sent_ms": marker.sent_ms}])

    def _drain(self) -> None:
        if self._resampler is not None:
            self._take(self._resampler.resample(None))
        self._resampler, self._source = None, None

    def _take(self, frames: list[av.AudioFrame]) -> None:
        for out in frames:
            self._rest += bytes(out.planes[0])[: out.samples * 2]
        while len(self._rest) >= self.CHUNK:
            chunk, self._rest = self._rest[: self.CHUNK], self._rest[self.CHUNK :]
            self._queue.put_nowait(AudioFrame(data=chunk, sample_rate=self.RATE))

    def clear(self) -> list[int]:
        unsent = [frame["utterance"] for frame in self._held]
        self._held = []
        kept: list[AudioFrame | _Marker] = []
        frames = 0
        while not self._queue.empty():
            item = self._queue.get_nowait()
            if isinstance(item, _Marker):
                if item.with_next:
                    unsent.extend([item.frame["utterance"]] if item.frame else [])
                elif frames < self.FADE_FRAMES:
                    kept.append(item)
            elif frames < self.FADE_FRAMES:
                kept.append(item)
                frames += 1
        while kept and isinstance(kept[-1], _Marker):
            kept.pop()
        self._rest = b""
        self._resampler, self._source = None, None
        self._start = None
        self._samples = 0
        n = 0
        for item in kept:
            if isinstance(item, _Marker):
                self._queue.put_nowait(item)
            else:
                self._queue.put_nowait(fade(item, 1 - n / frames, 1 - (n + 1) / frames))
                n += 1
        return sorted(set(unsent))

    def _fire(self, frames: list[dict[str, Any]]) -> None:
        if self._emit is not None:
            for frame in frames:
                self._emit(control(frame))

    async def recv(self) -> av.AudioFrame:
        while True:
            item = await self._queue.get()
            if not isinstance(item, _Marker):
                frame = item
                break
            item.sent_ms = self.sent_ms
            stamped = [{**item.frame, "sent_ms": item.sent_ms}] if item.frame else []
            if item.with_next:
                self._held.extend(stamped)
            else:
                held, self._held = self._held, []
                self._fire([*held, *stamped])
        now = time.monotonic()
        due = None if self._start is None else self._start + self._samples / self.RATE
        if due is None or now - due > self.GAP_S + self.PREBUFFER_S:
            self._start, self._samples, due = now - self.PREBUFFER_S, 0, now - self.PREBUFFER_S
            self._began = due if self._began is None else self._began
            self._pts = max(self._pts, int((due - self._began) * self.RATE))
        out = av.AudioFrame(format="s16", layout="mono", samples=len(frame.data) // 2)
        out.planes[0].update(frame.data)
        out.sample_rate = frame.sample_rate
        out.pts = self._pts
        out.time_base = fractions.Fraction(1, frame.sample_rate)
        self._pts += out.samples
        await asyncio.sleep(max(0.0, due - now))
        self._samples += out.samples
        self.sent_ms += self.FRAME_MS
        held, self._held = self._held, []
        self._fire(held)
        return out


class _Encoded(MediaStreamTrack):
    kind = "audio"

    def __init__(self, voice: _Voice) -> None:
        super().__init__()
        self._voice = voice
        self._encoder = OpusEncoder()

    async def recv(self) -> Any:
        while True:
            frame = await self._voice.recv()
            payloads, _ = await asyncio.to_thread(self._encoder.encode, frame)
            if payloads:
                packet = av.Packet(payloads[0])
                packet.pts = frame.pts
                packet.time_base = fractions.Fraction(1, frame.sample_rate)
                return packet


class WebRTCTransport:
    def __init__(
        self,
        identity: IdentityLike,
        scope: str,
        *,
        sample_rate: int = 16000,
        ice_servers: list[dict[str, Any]] | None = None,
    ) -> None:
        self.identity: Identity = as_identity(identity)
        self.scope = scope
        self.answer = ""
        self._rate = sample_rate
        self._pc = RTCPeerConnection(
            RTCConfiguration(iceServers=[RTCIceServer(**server) for server in ice_servers or []])
        )
        self._voice = _Voice(self._deliver)
        self._utterance: int | None = None
        self._ends: dict[int, _Marker] = {}
        self._refused = False
        self._cut: set[int] = set()
        self._pc.addTrack(_Encoded(self._voice))
        self._pc.getTransceivers()[0].setCodecPreferences(
            [c for c in RTCRtpSender.getCapabilities("audio").codecs if c.name == "opus"]
        )
        self._inbox: asyncio.Queue[AudioFrame | Image | Played | None] = asyncio.Queue()
        self._channel: RTCDataChannel | None = None
        self._unsent: list[str] = []
        self._readers: set[asyncio.Task[None]] = set()
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        self._jpeg: Any = None
        self._jpeg_size = (0, 0)
        self._greeted = False
        self._hello = asyncio.Event()
        self._closed = False

        @self._pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            self._read(self._hear(track) if track.kind == "audio" else self._see(track))

        @self._pc.on("datachannel")
        def on_channel(channel: RTCDataChannel) -> None:
            self._channel = channel

            @channel.on("message")
            def on_message(message: Any) -> None:
                if not isinstance(message, str):
                    return
                if not self._greeted:
                    try:
                        check_hello(message)
                    except InputError as exc:
                        self._refused = True
                        self._unsent.clear()
                        channel.send(control({"type": "error", "message": str(exc)}))
                        self._hello.set()
                        self._inbox.put_nowait(None)
                        return
                    self._greeted = True
                    self._hello.set()
                    self._flush()
                    return
                try:
                    played = parse_played(message)
                except InputError as exc:
                    logger.warning("transport: dropped a frame: %s", exc)
                    return
                if played.utterance not in self._cut:
                    self._inbox.put_nowait(played)

        @self._pc.on("connectionstatechange")
        def on_state() -> None:
            if self._pc.connectionState in ("failed", "closed"):
                self._hello.set()
                self._inbox.put_nowait(None)

    async def greeted(self) -> bool:
        await self._hello.wait()
        if not self._greeted:
            await self.close()
        return self._greeted

    async def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]:
        while (item := await self._inbox.get()) is not None:
            yield item

    async def send_audio(self, frame: AudioFrame, utterance: int) -> None:
        if utterance != self._utterance:
            self._switch(utterance)
        self._voice.push(frame)

    async def send_control(self, control_frame: dict[str, Any]) -> None:
        number = control_frame.get("utterance")
        match control_frame.get("type"):
            case "speaking":
                if self._utterance is not None:
                    self._switch(None)
                self._voice.mark(control_frame, with_next=True)
            case "silent":
                if number in self._ends:
                    self._voice.fill(self._ends.pop(number), control_frame)
                elif self._utterance is None or self._utterance == number:
                    self._voice.finish()
                    self._utterance = None
                    self._voice.mark(control_frame, with_next=False)
                else:
                    self._deliver(control({**control_frame, "sent_ms": self._voice.sent_ms}))
            case _:
                self._deliver(control(control_frame))

    def clear_audio(self) -> None:
        self._utterance = None
        self._ends.clear()
        for number in self._voice.clear():
            self._cut.add(number)
            self._inbox.put_nowait(Played(utterance=number, ms=0))

    def _switch(self, utterance: int | None) -> None:
        self._voice.finish()
        if self._utterance is not None:
            self._ends[self._utterance] = self._voice.mark(None, with_next=False)
        self._utterance = utterance

    def _deliver(self, text: str) -> None:
        if self._refused:
            return
        self._unsent.append(text)
        self._flush()

    def _flush(self) -> None:
        channel = self._channel
        if not self._greeted or channel is None or channel.readyState != "open":
            return
        for text in self._unsent:
            channel.send(text)
        self._unsent.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._hello.set()
        for reader in self._readers:
            reader.cancel()
        with contextlib.suppress(Exception):
            await self._pc.close()
        self._inbox.put_nowait(None)

    def _read(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._readers.add(task)
        task.add_done_callback(self._readers.discard)

    async def _hear(self, track: MediaStreamTrack) -> None:
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            if not isinstance(frame, av.AudioFrame):
                continue
            for out in self._resampler.resample(frame):
                self._inbox.put_nowait(
                    AudioFrame(data=bytes(out.planes[0])[: out.samples * 2], sample_rate=self._rate)
                )

    async def _see(self, track: MediaStreamTrack) -> None:
        loop = asyncio.get_running_loop()
        while True:
            try:
                frame = await track.recv()
            except MediaStreamError:
                return
            if not isinstance(frame, av.VideoFrame):
                continue
            jpeg = await loop.run_in_executor(None, self._encode, frame)
            if jpeg:
                self._inbox.put_nowait(Image(media_type="image/jpeg", data=jpeg))

    def _encode(self, frame: av.VideoFrame) -> bytes:
        if self._jpeg is None or self._jpeg_size != (frame.width, frame.height):
            codec = av.CodecContext.create("mjpeg", "w")
            if not isinstance(codec, av.VideoCodecContext):
                raise TransportError("webrtc: this build of av has no jpeg encoder")
            codec.width, codec.height = frame.width, frame.height
            codec.pix_fmt = "yuvj420p"
            codec.time_base = fractions.Fraction(1, 1000)
            codec.options = {"qmin": "5", "qmax": "5"}
            self._jpeg, self._jpeg_size = codec, (frame.width, frame.height)
        return b"".join(
            bytes(packet) for packet in self._jpeg.encode(frame.reformat(format="yuvj420p"))
        )


async def accept_offer(
    sdp: str,
    *,
    identity: IdentityLike,
    scope: str,
    sample_rate: int = 16000,
    ice_servers: list[dict[str, Any]] | None = None,
) -> WebRTCTransport:
    person = as_identity(identity)
    if not scope:
        raise InputError("transport: scope is empty")
    if "opus/48000" not in sdp.lower():
        raise TransportError("webrtc: the offer has no opus audio")
    transport = WebRTCTransport(person, scope, sample_rate=sample_rate, ice_servers=ice_servers)
    try:
        await transport._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await transport._pc.setLocalDescription(await transport._pc.createAnswer())
    except Exception as exc:
        await transport.close()
        raise TransportError(f"webrtc: the offer could not be answered: {exc}") from exc
    transport.answer = transport._pc.localDescription.sdp
    return transport

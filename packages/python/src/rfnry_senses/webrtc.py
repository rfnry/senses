from __future__ import annotations

import array
import asyncio
import contextlib
import fractions
import logging
import sys
import time
from collections.abc import AsyncIterator, Coroutine
from typing import Any

import av
from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCDataChannel,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)
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


class _Voice(MediaStreamTrack):
    kind = "audio"
    FRAME_MS = 20
    PREBUFFER_S = 0.12
    GAP_S = 0.1
    FADE_FRAMES = 3

    def __init__(self) -> None:
        super().__init__()
        self._queue: asyncio.Queue[AudioFrame] = asyncio.Queue()
        self._rest = b""
        self._pts = 0
        self._start: float | None = None
        self._samples = 0

    def push(self, frame: AudioFrame) -> None:
        self._rest += frame.data
        size = frame.sample_rate * self.FRAME_MS // 1000 * 2 * frame.channels
        while len(self._rest) >= size:
            chunk, self._rest = self._rest[:size], self._rest[size:]
            self._queue.put_nowait(
                AudioFrame(data=chunk, sample_rate=frame.sample_rate, channels=frame.channels)
            )

    def clear(self) -> None:
        tail: list[AudioFrame] = []
        while not self._queue.empty():
            frame = self._queue.get_nowait()
            if len(tail) < self.FADE_FRAMES:
                tail.append(frame)
        self._rest = b""
        self._start = None
        self._samples = 0
        steps = len(tail)
        for n, frame in enumerate(tail):
            self._queue.put_nowait(fade(frame, 1 - n / steps, 1 - (n + 1) / steps))

    async def recv(self) -> av.AudioFrame:
        frame = await self._queue.get()
        out = av.AudioFrame(
            format="s16",
            layout="mono" if frame.channels == 1 else "stereo",
            samples=len(frame.data) // (2 * frame.channels),
        )
        out.planes[0].update(frame.data)
        out.sample_rate = frame.sample_rate
        out.pts = self._pts
        out.time_base = fractions.Fraction(1, frame.sample_rate)
        self._pts += out.samples
        now = time.monotonic()
        due = None if self._start is None else self._start + self._samples / out.sample_rate
        if due is None or now - due > self.GAP_S + self.PREBUFFER_S:
            self._start, self._samples, due = now - self.PREBUFFER_S, 0, now - self.PREBUFFER_S
        await asyncio.sleep(max(0.0, due - now))
        self._samples += out.samples
        return out


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
        self._voice = _Voice()
        self._pc.addTrack(self._voice)
        self._inbox: asyncio.Queue[AudioFrame | Image | Played | None] = asyncio.Queue()
        self._channel: RTCDataChannel | None = None
        self._unsent: list[str] = []
        self._readers: set[asyncio.Task[None]] = set()
        self._resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)
        self._jpeg: Any = None
        self._jpeg_size = (0, 0)
        self._greeted = False
        self._closed = False

        @self._pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            self._read(self._hear(track) if track.kind == "audio" else self._see(track))

        @self._pc.on("datachannel")
        def on_channel(channel: RTCDataChannel) -> None:
            self._channel = channel
            for text in self._unsent:
                channel.send(text)
            self._unsent.clear()

            @channel.on("message")
            def on_message(message: Any) -> None:
                if not isinstance(message, str):
                    return
                if not self._greeted:
                    try:
                        check_hello(message)
                    except InputError as exc:
                        channel.send(control({"type": "error", "message": str(exc)}))
                        self._inbox.put_nowait(None)
                        return
                    self._greeted = True
                    return
                try:
                    self._inbox.put_nowait(parse_played(message))
                except InputError as exc:
                    logger.warning("transport: dropped a frame: %s", exc)

        @self._pc.on("connectionstatechange")
        def on_state() -> None:
            if self._pc.connectionState in ("failed", "closed"):
                self._inbox.put_nowait(None)

    async def inbound(self) -> AsyncIterator[AudioFrame | Image | Played]:
        while (item := await self._inbox.get()) is not None:
            yield item

    async def send_audio(self, frame: AudioFrame, utterance: int) -> None:
        self._voice.push(frame)

    async def send_control(self, control_frame: dict[str, Any]) -> None:
        text = control(control_frame)
        if self._channel is not None and self._channel.readyState == "open":
            self._channel.send(text)
        else:
            self._unsent.append(text)

    def clear_audio(self) -> None:
        self._voice.clear()

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
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
    transport = WebRTCTransport(person, scope, sample_rate=sample_rate, ice_servers=ice_servers)
    try:
        await transport._pc.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
        await transport._pc.setLocalDescription(await transport._pc.createAnswer())
    except Exception as exc:
        await transport.close()
        raise TransportError(f"webrtc: the offer could not be answered: {exc}") from exc
    transport.answer = transport._pc.localDescription.sdp
    return transport
